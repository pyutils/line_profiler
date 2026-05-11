"""
A simple :py:deco:`pytest.mark.retry` decorator.
Function-scoped fixtures are re-fetched between retries.

Note:
    - This file is designed to also function as a standalone
      ``conftest.py`` file.

    - Adapted from `pytest-mark-retry`_.

.. _pytest-mark-retry: https://gitlab.com/TTsangSC/pytest-mark-retry
"""
from __future__ import annotations

import ast
import dataclasses
import os
import sys
import warnings
from collections.abc import (
    Callable, Collection, Generator, Hashable, Iterable, Mapping,
)
from functools import cached_property, lru_cache, partial
from importlib.util import find_spec
from inspect import Signature, signature
from operator import contains
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any, ClassVar, Literal, Protocol, TypedDict, TypeVar, cast, overload,
)
from typing_extensions import Self

import pytest
from _pytest.compat import NOTSET
from _pytest.fixtures import SubRequest
from _pytest.nodes import Node
from _pytest.scope import Scope
from _pytest.unittest import TestCaseFunction
try:
    from pytest import TerminalReporter  # type: ignore
except ImportError:  # pytest < ~8.4
    from _pytest.terminal import TerminalReporter  # type: ignore


__all__ = (
    'RetryMarker',
    'RetryMarkerWarning',
    'RetryConditionFailure',
    'pytest_addhooks',
    'pytest_configure',
    'pytest_terminal_summary',
)

_Status = Literal['passed', 'failed', 'skipped']
_Require = Literal['any', 'all']
F = TypeVar('F', bound=TestCaseFunction)
FCls = TypeVar('FCls', bound='type[TestCaseFunction]')
T = TypeVar('T')

_FUNCTION_SCOPE = Scope.Function


class _PyfuncCallImpl(Protocol):
    def __call__(self, *, pyfuncitem: pytest.Function) -> Any:
        ...


class _RetryError(RuntimeError):
    """
    Base class for errors associated with retrying tests.
    """


class RetryMarkerWarning(_RetryError, UserWarning):
    """
    Warning issued when the :deco:`pytest.mark.retry` markers on a test
    fail to resolve to a valid :py:class:`RetryMarker` instance.
    """
    @classmethod
    def warn_from_error(
        cls, xc: Exception, *args, **kwargs
    ) -> None:
        msg = 'disregarding invalid `@pytest.mark.retry` marker'
        msg = f'{msg}: ({_format_exception(xc)})'
        if 'PYTEST_CURRENT_TEST' in os.environ:
            msg = f'{os.environ["PYTEST_CURRENT_TEST"]}: {msg}'
        if sys.version_info < (3, 12):  # Compatibility
            kwargs.pop('skip_file_prefixes', None)
        return warnings.warn(msg, cls, *args, **kwargs)


class RetryConditionFailure(_RetryError):
    """
    Error raised when an attempt to retry a test failed because we
    can't :py:func:`eval` the condition.
    """
    def __init__(
        self,
        previous_error: Exception,
        condition_error: Exception,
        # Should't be here if the condition isn't a string, but whatever
        condition: str | bool | None = None
    ) -> None:
        self.previous_error = previous_error
        self.condition_error = condition_error
        self.condition = condition
        super().__init__(self._format_message())

        condition_error.__cause__ = previous_error
        self.__cause__ = condition_error

    def _format_message(self) -> str:
        prev = _format_exception(self.previous_error)
        if len(prev.split()) > 1:
            prev = f'({prev})'
        condition = _format_exception(self.condition_error)
        if self.condition:
            condition = f'(condition: {self.condition!r} -> {condition})'
        return '{} -> {}'.format(prev, condition)


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
    def add_entry(cls, func: pytest.Function, *args, **kwargs) -> Self:
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


def _retry_marker_sig_helper(
    retries=1, *,
    exceptions=None, reset_fixtures=None, condition=None, require=None,
):
    """
    Dummy callable helping with :py:meth:`RetryMarker.from_test_func`,
    so that we can handle marker stacking in a sane way withou having to
    special-case the defaults of the class constructor.
    """
    pass


if sys.version_info >= (3, 10):
    _keyword = partial(dataclasses.field, kw_only=True)
else:
    _keyword = dataclasses.field


class _RetryMarkerArgs(TypedDict, total=False):
    retries: int
    exceptions: type[Exception] | tuple[type[Exception], ...]
    reset_fixtures: bool | Collection[str]
    condition: str | bool | None
    require: _Require


@dataclasses.dataclass
class RetryMarker:
    """
    Object representing the :deco:`pytest.mark.retry` marks on a test
    function, managing test retries.

    Attributes:
        retries (int):
            Number of retries to attempt; should be positive.
        exceptions (type[Exception] | tuple[type[Exception], ...]):
            "Allowed" exception type(s) which result in retries;
            mismatching exceptions are propagated normally, resuling in
            a failure
        reset_fixtures (bool | Collection[str]):
            Whether to reset the function-scoped fixtures when retrying;
            if a collection of names, only reset matching fixtures
        condition (str | bool | None):
            Only attempt retries if this is true (or :py:const:`None`);
            if a string, it is :py:func:`eval`-ed to the condition
            before each retry using the globals of the test function,
            and the fixtures and parametrizations as the locals
        require (Literal['any', 'all']):
            Whether 'any' or 'all' attempts to run a test function
            should pass for the test to pass

    See also:
        :py:meth:`.from_arguments` for examples
    """
    retries: int = 1
    exceptions: type[Exception] | tuple[type[Exception], ...] = (
        _keyword(default=Exception)
    )
    reset_fixtures: bool | Collection[str] = _keyword(default=True)
    condition: str | bool | None = _keyword(default=None)
    require: _Require = _keyword(default='any')

    name: ClassVar[str] = 'retry'
    _sig: ClassVar[Signature] = signature(_retry_marker_sig_helper)

    def __post_init__(self) -> None:
        # Normalize `.retries`
        try:
            self.retries = max(0, int(self.retries))
        except Exception as e:
            msg = f'.retries = {self.retries!r}'
            msg = f'{msg}: not a valid number {_format_exception(e)}'
            raise TypeError(msg).with_traceback(e.__traceback__)
        # Check `.exceptions`
        if isinstance(self.exceptions, tuple):
            xc: tuple[type[Exception], ...] = self.exceptions
        else:
            xc = self.exceptions,
        if not all(
            isinstance(X, type) and issubclass(X, Exception) for X in xc
        ):
            raise TypeError(
                f'.exceptions = {self.exceptions!r}: '
                'expected an exception type or a tuple thereof'
            )
        # Check `.condition`
        if isinstance(self.condition, str):
            try:
                ast.parse(self.condition, mode='eval')
            except Exception as e:
                msg = f'.condition = {self.condition!r}'
                msg = f'{msg}: not a valid expression ({_format_exception(e)})'
                raise ValueError(msg).with_traceback(e.__traceback__)
        # Check `.require`
        if self.require not in ('all', 'any'):
            msg = f'.require = {self.require!r}: expected \'any\' or \'all\''
            raise TypeError(msg)

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
                    raise RetryConditionFailure(xc, cond, self.condition)
                if not cond:
                    i -= 1
                    break
            try:
                result = impl(pyfuncitem=func)
            except self.exceptions as e:
                # `ty` doesn't agree that `e` is an exception (#3432)...
                xc = cast(Exception, e)
                if self.require == 'all':
                    break
            except Exception as e:  # Uncaught exc. -> break to raise
                xc = e
                break
            else:  # Correct execution -> break to return
                xc = None
                if self.require == 'any':
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
        condition: str | bool | None, func: pytest.Function,
    ) -> tuple[Any, Literal[False]] | tuple[Exception, Literal[True]]:
        if condition in (True, None):  # Always retry
            return (True, False)
        if condition in (False,):  # Never retry
            return (False, False)

        if TYPE_CHECKING:  # Help narrowing
            assert isinstance(condition, str)
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
        pytest_pyfunc_call: _PyfuncCallImpl = pm.subset_hook_caller(
            'pytest_pyfunc_call', [cls],
        )
        try:
            helper = cls.from_test_func(pyfuncitem)
        except Exception as e:
            # Level 1 is the `.warn_from_error()` frame, 2 is here, 3 is
            # where the error actually happened
            warn = RetryMarkerWarning.warn_from_error
            skip_ = {_find_module_path('_pytest'), _find_module_path('pluggy')}
            skip = cast(tuple[str, ...], tuple(skip_ - {None}))
            warn(e, stacklevel=3, skip_file_prefixes=skip)
        else:
            if helper.is_active:
                return helper.manage_call(pytest_pyfunc_call, pyfuncitem)
        return pytest_pyfunc_call(pyfuncitem=pyfuncitem)

    @classmethod
    def from_test_func(cls, func: pytest.Function, /) -> Self:
        """
        Returns:
            Instance combining the stack of :deco:`pytest.mark.retry`
            decorators on the :py:class:`pytest.Function`.
        """
        marks = (m for m in func.iter_markers() if m.name == cls.name)
        return cls.from_arguments(cls._get_marker_args(mark) for mark in marks)

    @classmethod
    @overload
    def from_arguments(cls, args: Iterable[_RetryMarkerArgs] = (), /) -> Self:
        ...

    @classmethod
    @overload
    def from_arguments(cls, *args: _RetryMarkerArgs) -> Self:
        ...

    @classmethod
    def from_arguments(cls, *args) -> Self:
        """
        Invocations:
            (<arg_dict>, ...) -> RetryMarker
            ([<arg_dict>, ...]) -> RetryMarker

        Examples:
            >>> empty = RetryMarker.from_arguments()
            >>> assert not empty.retries
            >>> assert empty == RetryMarker.from_arguments([])

            >>> default = RetryMarker.from_arguments({})
            >>> assert default.retries == 1
            >>> assert default.exceptions in ((Exception,), Exception)
            >>> assert default.reset_fixtures == True
            >>> assert default.condition is None
            >>> assert default.require == 'any'
            >>> assert default.is_active
            >>> assert default == RetryMarker.from_arguments([{}])

            Some arguments result in inactive instances (i.e. no
            retries):

            >>> bad_xc = RetryMarker.from_arguments({'exceptions': ()})
            >>> assert not bad_xc.exceptions
            >>> assert not bad_xc.is_active

            >>> bad_retries = RetryMarker.from_arguments(
            ...     {}, {}, {'retries': -5},
            ... )
            >>> assert not bad_retries.retries
            >>> assert not bad_retries.is_active

            >>> bad_cond = RetryMarker.from_arguments(
            ...     {'condition': False},
            ... )
            >>> assert bad_cond.condition == False
            >>> assert not bad_cond.is_active

            Congruent values are unioned:

            >>> stacked_xcs = RetryMarker.from_arguments([
            ...     {'exceptions': ()},
            ...     {'exceptions': ValueError},
            ...     {'retries': 3, 'exceptions': (TypeError, OSError)},
            ... ])
            >>> assert stacked_xcs.retries == 5
            >>> assert set(stacked_xcs.exceptions) == {
            ...     ValueError, TypeError, OSError,
            ... }

            >>> stacked_resets_1 = RetryMarker.from_arguments(
            ...     {'reset_fixtures': ['foo', 'bar']},
            ...     {'reset_fixtures': ['baz']},
            ... )
            >>> sorted(stacked_resets_1.reset_fixtures)
            ['bar', 'baz', 'foo']

            Incongruent values override one another:

            >>> stacked_resets_2 = RetryMarker.from_arguments(
            ...     {'reset_fixtures': ['foo', 'bar']},
            ...     {'reset_fixtures': False},
            ... )
            >>> assert stacked_resets_2.reset_fixtures == False

            >>> stacked_conditions = RetryMarker.from_arguments(
            ...     {'condition': 'foo==bar'},
            ...     {'condition': False},
            ... )
            >>> assert stacked_conditions.condition == False
            >>> assert not stacked_conditions.is_active

            >>> stacked_requires = RetryMarker.from_arguments(
            ...     {'require': 'any'},
            ...     {'require': 'all'},
            ... )
            >>> assert stacked_requires.require == 'all'
        """
        retries: int = 0
        xc: set[type[Exception]] | None = None
        reset_fixtures: bool | set[str] = True
        condition: bool | str | None = None
        require: _Require | None = None

        if args:
            if isinstance(args[0], Mapping):
                iter_args = args
            else:
                assert len(args) == 1
                iter_args = args[0]
        else:
            iter_args = args

        # `ty` needs some help here... hence the `cast()`
        for bound_args in cast(Iterable[_RetryMarkerArgs], iter_args):
            retries += bound_args.get('retries', 1)
            if 'exceptions' in bound_args:
                xc_new = bound_args['exceptions']
                if xc is None:
                    xc = set()
                if isinstance(xc_new, type):
                    xc.add(xc_new)
                else:
                    xc.update(xc_new)
            if 'reset_fixtures' in bound_args:
                rf_new = bound_args['reset_fixtures']
                if isinstance(rf_new, Collection):
                    if isinstance(reset_fixtures, Collection):
                        reset_fixtures.update(rf_new)
                    else:
                        reset_fixtures = set(rf_new)
                else:
                    reset_fixtures = bool(rf_new)
            if 'condition' in bound_args:
                condition = bound_args['condition']
            if 'require' in bound_args:
                require = bound_args['require']

        kwargs: _RetryMarkerArgs = {
            'retries': retries, 'reset_fixtures': reset_fixtures,
        }
        if xc is not None:
            kwargs['exceptions'] = tuple(xc)
        if condition is not None:
            kwargs['condition'] = condition
        if require is not None:
            kwargs['require'] = require
        return cls(**kwargs)

    @classmethod
    def _get_marker_args(cls, mark: pytest.Mark) -> _RetryMarkerArgs:
        args = cls._sig.bind(*mark.args, **mark.kwargs).arguments
        return cast(_RetryMarkerArgs, args)

    @property
    def is_active(self) -> bool:
        """
        Whether the instance should possibly attempt retries in any
        condition
        """
        if not self.retries:
            return False
        if self.exceptions == ():
            return False
        if self.condition in (False,):
            return False
        return True


def _pluralize(noun: str, count: int, plural: str | None = None) -> str:
    if plural is None:
        plural = noun + 's'
    return f'{count} {noun if count == 1 else plural}'


def _format_exception(xc: Exception) -> str:
    msg = type(xc).__name__
    if str(xc):
        return f'{msg}: {xc}'
    return msg


@lru_cache()
def _find_module_path(module: str) -> str | None:
    spec = find_spec(module)
    if spec is None or spec.origin is None:
        return None
    file = Path(spec.origin)
    if not file.exists():
        return None
    if file.name == '__init__.py':  # Package
        file = file.parent
    return str(file)


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    """
    Register :py:class:`RetryMarker` as a plugin so that its
    :py:meth:`pytest_pyfunc_call` method can safely call other
    implementations without recursing to itself.
    """
    pluginmanager.register(RetryMarker)


def pytest_configure(config: pytest.Config) -> None:
    """
    Register the :py:deco:`pytest.mark.retry` marker.
    """
    help_text = ' '.join("""
    retry(retries=1, *, exceptions=Exception, \
reset_fixtures=True, condition=None, require='any'):

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
            and parametrizations);
        require (Literal['any', 'all']):
            If 'any', stop retrying and record a test pass if ANY
            attempt passes;
            if 'all', only record a test pass if ALL attempts pass
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
