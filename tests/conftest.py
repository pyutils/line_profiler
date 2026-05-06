"""
A simple :py:deco:`pytest.mark.retry` decorator.
Function-scoped fixtures are re-fetched bewteen retries.
"""
from __future__ import annotations

import dataclasses
import warnings
from collections.abc import (
    Callable, Collection, Generator, Hashable, Iterable, Mapping,
)
from functools import cached_property, partial
from operator import contains
from pathlib import Path
from typing import (
    TYPE_CHECKING, Any, ClassVar, Literal, Protocol, TypeVar, cast, final,
)

import pytest
from _pytest.compat import NOTSET
from _pytest.fixtures import SubRequest
from _pytest.nodes import Node
from _pytest.scope import Scope
from _pytest.unittest import TestCaseFunction
try:
    from pytest import TerminalReporter
except ImportError:  # pytest < ~8.4
    from _pytest.terminal import TerminalReporter


_Status = Literal['passed', 'failed', 'skipped']
F = TypeVar('F', bound=TestCaseFunction)
FCls = TypeVar('FCls', bound=type[TestCaseFunction])
T = TypeVar('T')

_FUNCTION_SCOPE = Scope.Function


class _PyfuncCallImpl(Protocol):
    def __call__(self, *, pyfuncitem: pytest.Function) -> Any:
        ...


class _RetryFailure(RuntimeError):
    def __init__(
        self,
        previous_error: Exception,
        condition_error: Exception,
        condition: str | None = None
    ) -> None:
        self.previous_error = previous_error
        self.condition_error = condition_error
        self.condition = condition
        super().__init__(self._format_message())

        condition_error.__cause__ = previous_error
        self.__cause__ = condition_error

    def _format_message(self) -> str:
        prev = self._format_exception(self.previous_error)
        if len(prev.split()) > 1:
            prev = f'({prev})'
        condition = self._format_exception(self.condition_error)
        if self.condition:
            condition = f'(condition: {self.condition!r} -> {condition})'
        return '{} -> {}'.format(prev, condition)

    @staticmethod
    def _format_exception(xc: Exception) -> str:
        msg = type(xc).__name__
        if str(xc):
            return f'{msg}: {xc}'
        return msg


@final
@dataclasses.dataclass
class _RetryEntry:
    func: pytest.Function
    retries: int
    status: _Status

    def _get_name(self, with_params: bool) -> str:
        path, prefix = self._name_prefixes
        if with_params:
            name = self.func.name
        else:
            name = self.func.originalname
        if prefix:
            name = f'{prefix}.{name}'
        if path:
            name = f'{path}::{name}'
        return name

    @classmethod
    def add_entry(cls, func: pytest.Function, *args, **kwargs) -> _RetryEntry:
        assert func.config is not None
        entry = cls(func, *args, **kwargs)
        entry.get_entries(func.config).append(entry)
        return entry

    @staticmethod
    def get_entries(config: pytest.Config) -> list[_RetryEntry]:
        return config.stash.setdefault(_RETRY_ENTRIES_KEY, [])

    @property
    def full_name(self) -> str:
        return self._get_name(True)

    @property
    def full_original_name(self) -> str:
        return self._get_name(False)

    @cached_property
    def _name_prefixes(self) -> tuple[str, str]:
        chunks: list[str] = []
        node: Node | None = self.func.parent
        path = ''
        seen: set[int] = set()
        while True:
            if id(node) in seen:
                break
            else:
                seen.add(id(node))
            if isinstance(node, (pytest.Module, pytest.Package)):
                if node.path:
                    npath = node.path
                    try:
                        npath = npath.relative_to(Path.cwd())
                    except ValueError:  # Not a subpath
                        pass
                    path = str(npath)
                else:
                    path = repr(node)
                break
            name: str | None = getattr(node, 'name', None)
            if not name:
                break
            chunks.append(name)
        return path, '.'.join(reversed(chunks))


_RETRY_ENTRIES_KEY = pytest.StashKey[list[_RetryEntry]]()
_NEXTITEMS_KEY = pytest.StashKey[dict[pytest.Item, pytest.Item | None]]()


@final
@dataclasses.dataclass
class _RetryHelper:
    retries: int = 1
    exceptions: type[Exception] | tuple[type[Exception], ...] = ()
    reset_fixtures: bool | Collection[str] = True
    condition: str | None = None
    name: ClassVar[str] = 'retry'

    def __post_init__(self) -> None:
        if not (int(self.retries) == self.retries > 0):
            raise TypeError(
                f'.entries = {self.retries!r}: expected a positive integer'
            )
        if isinstance(self.exceptions, tuple):
            xc: tuple[type[Exception], ...] = self.exceptions
        else:
            xc = self.exceptions,
        if not all(issubclass(X, Exception) for X in xc):
            raise TypeError(
                f'.exceptions = {self.exceptions!r}: '
                'expected an exception type or a tuple thereof'
            )

    def manage_call(self, impl: _PyfuncCallImpl, func: pytest.Function) -> Any:
        """
        Manage the call(s) to a function.

        Args:
            impl (Callable):
                Implementation of
                ``pytest_pyfunc_call(pyfuncitem: pytest.Function) \
-> Any``
            func (pytest.Function):
                Test function item

        Returns:
            Value of the first successful call to
            ``pytest_pyfunc_call(pyfuncitem=func)``
        """
        check_fixture_name: Callable[[str], bool]
        reset_fixtures: Callable[[pytest.Function], None]
        if self.reset_fixtures:
            if isinstance(self.reset_fixtures, Collection):
                check_fixture_name = partial(contains, self.reset_fixtures)
            else:
                check_fixture_name = lambda _: True  # noqa: E731
            reset_fixtures = partial(
                self._reset_between_retries,
                reset_fixtures=True,
                should_reset=check_fixture_name,
            )
        else:
            reset_fixtures = self._reset_between_retries

        result: Any = None
        xc: Exception | None = None
        for i in range(1 + self.retries):
            if i:
                reset_fixtures(func)
                if self.condition:
                    cond, error = self._check_condition(self.condition, func)
                    if error:  # Bail
                        # XXX: would be nice if we can directly force an
                        # internal error, but that doesn't seem to be
                        # possible from within `pytest_pyfunc_call()`;
                        # directly calling `pytest_internalerror()`
                        # results in botched teardown and weird
                        # tracebacks, and leaves the test session in a
                        # bad state...
                        assert xc is not None
                        raise _RetryFailure(xc, cond, self.condition)
                    if not cond:
                        i -= 1
                        break
            try:
                result = impl(pyfuncitem=func)
            except self.exceptions as e:
                # `ty` doesn't agree that `e` is an exception...
                xc = cast(Exception, e)
            except Exception as e:  # Uncaught exc. -> break to raise
                xc = e
                break
            else:  # Correct execution -> break to return
                xc = None
                break
        if i:
            if xc is None:
                status: _Status = 'passed'
            elif isinstance(xc, pytest.skip.Exception):
                status = 'skipped'
            else:
                status = 'failed'
            _RetryEntry.add_entry(func, i, status)
        if xc is None:
            return result
        else:
            raise xc

    @staticmethod
    def _check_condition(
        condition: str, func: pytest.Function,
    ) -> tuple[Any, Literal[False]] | tuple[Exception, Literal[True]]:
        global_ns: dict[str, Any] | None = None
        try:
            global_ns = func.obj.__globals__
        except AttributeError:  # Not a `types.FunctionType`
            pass
        local_ns = func.funcargs
        try:
            return (eval(condition, global_ns, local_ns), False)
        except Exception as e:
            return (e, True)

    @staticmethod
    def _reset_between_retries(
        func: pytest.Function,
        reset_fixtures: bool = False,
        should_reset: Callable[[str], bool] = lambda _: False,
    ) -> None:
        """
        Note:
            This makes HEAVY use of :py:mod`_pytest` internals.
        """
        def cleanup_fixture(fdef: pytest.FixtureDef[Any]) -> None:
            if not (
                fdef.scope == 'function'
                and should_reset(fdef.argname)
                and getattr(fdef, 'cached_result', None) is not None
            ):
                return
            fdef.cached_result = None
            finalize(fdef)

        def finalize(fdef: pytest.FixtureDef[Any]) -> None:
            assert fdef.scope == 'function'

            # Plagiarized code from
            # `FixtureRequest._get_active_fixture_def()`
            try:
                callspec = func.callspec
            except AttributeError:
                callspec = None
            if callspec is not None and fdef.argname in callspec.params:
                value = callspec.params[fdef.argname]
                index = callspec.indices[fdef.argname]
            else:
                value, index = NOTSET, 0

            with warnings.catch_warnings():
                warnings.simplefilter(
                    'ignore', pytest.PytestDeprecationWarning,
                )
                fdef.finish(SubRequest(
                    request=func._request,
                    scope=_FUNCTION_SCOPE,
                    param=value,
                    param_index=index,
                    fixturedef=fdef,
                ))

        def unique(
            items: Iterable[T], key: Callable[[T], Hashable] = id,
        ) -> Generator[T, None, None]:
            seen: set[Hashable] = set()
            for item in items:
                hashed = key(item)
                if hashed in seen:
                    continue
                seen.add(hashed)
                yield item

        def iter_all_fixture_defs(
            func: pytest.Function,
        ) -> Generator[pytest.FixtureDef[Any], None, None]:
            fdef_mapping: Mapping[Any, Iterable[pytest.FixtureDef[Any]]]
            # Somehow `mypy` doesn't trust the below but `ty` does...
            for fdef_mapping in [  # type:ignore[assignment]
                func._fixtureinfo.name2fixturedefs,
                func._request._arg2fixturedefs,
                func.session._fixturemanager._arg2fixturedefs,
            ]:
                for fixture_defs in fdef_mapping.values():
                    yield from fixture_defs

        if reset_fixtures:
            # Beside clearing `.funcargs`, `._initrequest()` also resets
            # the `TopRequest` instance that `func` has (`._request`)
            func._initrequest()
            for fixture_def in unique(iter_all_fixture_defs(func)):
                cleanup_fixture(fixture_def)
        else:
            # Fixture values will naturally refill, possibly from caches
            func.funcargs.clear()
        func.setup()

    @classmethod
    def pytest_pyfunc_call(cls, pyfuncitem: pytest.Function) -> Any:
        """
        Run the :py:class:`pytest.Function` object with the requisite
        number of retries if necessary.
        """
        pm = pyfuncitem.config.pluginmanager
        impl: _PyfuncCallImpl = pm.subset_hook_caller(
            'pytest_pyfunc_call', [cls],
        )
        helper = cls.get_helper(pyfuncitem)
        if helper:
            return helper.manage_call(impl, pyfuncitem)
        return impl(pyfuncitem=pyfuncitem)

    @classmethod
    def get_helper(cls, pyfuncitem: pytest.Function) -> _RetryHelper | None:
        retries: int = 0
        xc: set[type[Exception]] = set()
        reset_fixtures: bool | set[str] = True
        condition: bool | str | None = None
        for mark in pyfuncitem.iter_markers():
            if mark.name != cls.name:
                continue
            instance = cls(*mark.args, **mark.kwargs)
            retries += instance.retries
            condition = instance.condition
            if isinstance(instance.exceptions, tuple):
                xc.update(instance.exceptions)
            else:
                xc.add(instance.exceptions)
            if (
                reset_fixtures not in (True, False)
                and instance.reset_fixtures not in (True, False)
            ):  # Both collections of fixture names
                if TYPE_CHECKING:
                    assert not isinstance(reset_fixtures, bool)
                    assert not isinstance(instance.reset_fixtures, bool)
                reset_fixtures.update(instance.reset_fixtures)
            elif instance.reset_fixtures not in (True, False):
                if TYPE_CHECKING:
                    assert not isinstance(instance.reset_fixtures, bool)
                reset_fixtures = set(instance.reset_fixtures)
            else:
                reset_fixtures = bool(instance.reset_fixtures)
        if not retries:
            return None
        if not (condition is None or isinstance(condition, str)):
            if condition:
                condition = None
            else:
                return None
        if not xc:
            xc = {Exception}
        return cls(retries, tuple(xc), reset_fixtures, condition)


def _pluralize(noun: str, count: int, plural: str | None = None) -> str:
    if plural is None:
        plural = noun + 's'
    return f'{count} {noun if count == 1 else plural}'


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    """
    Register :py:class:`_RetryHelper` as a plugin so that its
    :py:meth:`pytest_pyfunc_call` method can safely call other
    implementations without recursing to itself.
    """
    pluginmanager.register(_RetryHelper)


def pytest_configure(config: pytest.Config) -> None:
    """
    Register the :py:deco:`pytest.mark.retry` marker.
    """
    help_text = ' '.join("""
    retry(retries=1, exceptions=Exception, \
reset_fixtures=True, condition=None):

    mark the test for retrying upon failure.

    Args (all optional):
        retries (int):
            Max number of retries for the (sub-)test;
        exceptions (type[Exception] | tuple[type[Exception], ...]):
            Error types which trigger a retry when caught;
        reset_fixtures (bool | Collection[str]):
            Names of function-scoped fixtures to reset between retries,
            `True` reset all such fixtures,
            `False` none thereof;
        condition (bool | str | None):
            Optional condition for retry:
            if a boolean, only retry if true;
            if a string, only retry if it `eval()`s true
            (w/globals of the test module and locals from the fixtures
            and parametrizations)
    """.split())
    config.addinivalue_line('markers', help_text)


def pytest_terminal_summary(
    terminalreporter: TerminalReporter, config: pytest.Config,
) -> None:
    """
    Write a summary section about rerun tests.
    """
    def get_summary(status: _Status, entries: list[_RetryEntry]) -> str:
        return f'{_pluralize("test", len(entries))} {status} with retries'

    def group_subtests(
        entries: list[_RetryEntry]
    ) -> dict[str, list[_RetryEntry]]:
        result: dict[str, list[_RetryEntry]] = {}
        for entry in entries:
            result.setdefault(entry.full_original_name, []).append(entry)
        return result

    formatting = {'yellow': True}
    write_line: Callable[[str], None] = partial(
        terminalreporter.write_line, **formatting  # type: ignore
    )
    write_header = partial(
        terminalreporter.write_sep, '=', 'retries summary', **formatting
    )
    write_newline = partial(write_line, '')
    try:
        verbosity: int = config.get_verbosity()  # type: ignore
    except AttributeError:  # pytest < 8.0
        verbosity = int(config.option.verbose)
    retry_entries: dict[_Status, list[_RetryEntry]] = {}
    for entry in _RetryEntry.get_entries(config):
        retry_entries.setdefault(entry.status, []).append(entry)
    if not retry_entries:
        return
    if verbosity > 0:
        write_newline()
        write_header()
        write_newline()
        for status, entries in retry_entries.items():
            write_line(get_summary(status, entries) + ':')
            for entry in entries:
                write_line(
                    f'  {entry.full_name}: '
                    f'retried {_pluralize("time", entry.retries)}'
                )
        write_newline()
    else:
        write_header()
        for status, entries in retry_entries.items():
            tests: list[str] = []
            for name, children in group_subtests(entries).items():
                if len(children) == 1:
                    tests.append(children[0].full_name)
                else:
                    msg = f'{name} ({_pluralize("subtest", len(children))})'
                    tests.append(msg)
            write_line(f'{get_summary(status, entries)}: {", ".join(tests)}')
