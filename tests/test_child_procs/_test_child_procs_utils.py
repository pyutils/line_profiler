from __future__ import annotations

import dataclasses
import enum
import inspect
import itertools
import multiprocessing.pool
import operator
import os
import pickle
import re
import shlex
import subprocess
import sys
import sysconfig
import threading
import traceback
import warnings
from abc import ABC, abstractmethod
from argparse import ArgumentError
from collections.abc import (
    Callable, Collection, Generator, Iterable, Iterator, Mapping,
    Sequence, Set,
)
from contextlib import ExitStack
from functools import lru_cache, partial, wraps
from io import BytesIO
from importlib import import_module
from multiprocessing.pool import (  # type: ignore
    ExceptionWithTraceback as ExceptionHelper,
)
from numbers import Real
from pathlib import Path
from textwrap import dedent, indent
from time import monotonic
from types import MappingProxyType, ModuleType, TracebackType
from typing import (
    TYPE_CHECKING, Any, Generic, IO, Literal, Protocol, TypeVar,
    cast, final, overload,
)
from typing_extensions import Concatenate, Self, ParamSpec
from uuid import uuid4

import pytest
import ubelt as ub

from _line_profiler_hooks import load_pth_hook
from kernprof import main as kernprof_main
from line_profiler._child_process_profiling.cache import LineProfilingCache
from line_profiler._child_process_profiling.multiprocessing_patches import (
    _PATCHED_MARKER as MP_PATCHED_MARKER, _PATCHES as _MP_PATCHES,
)
from line_profiler.autoprofile.util_static import modpath_to_modname
from line_profiler.cleanup import Cleanup
from line_profiler.line_profiler import LineProfiler, LineStats
from line_profiler.toml_config import ConfigSource


__all__ = (
    'DEBUG', 'DEFAULT_TIMEOUT', 'NOT_SUPPLIED', 'START_METHODS',
    'PATCH_SUMMARIES',
    'StartMethod', 'ModuleFixture', 'Params',
    'ResultMismatch', 'TestTimeout',
    'preserve_object_attrs', 'preserve_targets',
    'cleanup_extra_pth_files', 'add_timeout',
    'CheckWarnings',
    'mp_patch_is_internal', 'get_mp_patches_toml_text',
    'summarize_mp_patches', 'filter_mp_patch_summary',
    'concat_command_line', 'run_subproc',
    'run_module', 'run_script', 'run_literal_code', 'check_tagged_line_nhits',
    'strip', 'search_cache_logs',
)


T = TypeVar('T')
T1 = TypeVar('T1')
T2 = TypeVar('T2')
TCtx_ = TypeVar('TCtx_')
PS = ParamSpec('PS')
C = TypeVar('C', bound=Callable[..., Any])
StartMethod = Literal['spawn', 'fork', 'forkserver', 'dummy']

START_METHODS = set(multiprocessing.get_all_start_methods())

DEFAULT_TIMEOUT = 5  # Seconds
DEBUG = True

# ======================== Misc. helper classes ========================


class _NotSupplied(enum.Enum):
    NOT_SUPPLIED = enum.auto()


NOT_SUPPLIED = _NotSupplied.NOT_SUPPLIED


class _GetAttr(Protocol):
    """
    Function signature for functions that behave like
    :py:func:`getattr``.
    """
    @overload
    def __call__(self, obj: Any, attr: str, /) -> Any:
        ...

    @overload
    def __call__(self, obj: Any, attr: str, default: Any, /) -> Any:
        ...

    def __call__(self, *args):
        ...


@final
class ResultMismatch(ValueError):
    def __init__(
        self,
        expected: Any,
        actual: Any | _NotSupplied = NOT_SUPPLIED,
        _trunc_tb: int = 0,
    ) -> None:
        if actual == NOT_SUPPLIED:
            msg = f'expected: {expected}'
        else:
            msg = f'expected {expected}, got {actual}'
        super().__init__(msg)
        self.expected = expected
        self.actual = actual
        self._trunc_tb = max(0, _trunc_tb)

    @classmethod
    def compare(
        cls, expected_: T1, actual_: T2, /, *,
        comparator: Callable[[T1, T2], bool] = operator.eq,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        if comparator(expected_, actual_):
            return
        raise cls(
            expected_ if expected is None else expected,
            actual_ if actual is None else actual,
            _trunc_tb=1,
        )

    @classmethod
    def pytest_runtest_makereport(
        cls, item: pytest.Item, call: pytest.CallInfo,
    ) -> Any:
        """
        Truncate the tracebacks of instances so that pytest outputs are
        more useful and actually stops at the frame where the comparends
        are shown.
        """
        impl: Callable[..., Any]
        impl = item.config.pluginmanager.subset_hook_caller(
            'pytest_runtest_makereport', [cls],
        )
        make_report = partial(impl, item=item, call=call)

        xc = call.excinfo
        if xc is None:
            return make_report()
        if not (isinstance(xc.value, cls) and xc.value._trunc_tb):
            return make_report()

        tb_stack: list[TracebackType] = [xc.tb]
        while tb_stack[-1].tb_next:
            tb_stack.append(tb_stack[-1].tb_next)
        if len(tb_stack) <= xc.value._trunc_tb:
            return make_report()
        tb_stack[-(xc.value._trunc_tb + 1)].tb_next = None

        del tb_stack  # Help the GC
        call.excinfo = xc.from_exception(xc.value.with_traceback(xc.tb))
        return make_report(call=call)

    @property
    def rich_message(self) -> str:
        msg = '{}: {}'.format(type(self).__name__, self.args[0])
        if self.__traceback__ is not None:
            tb = self.__traceback__
            msg = '{}:{}: {}'.format(
                tb.tb_frame.f_code.co_filename, tb.tb_lineno, msg,
            )
        return msg


class TestTimeout(RuntimeError):
    """
    Error raised by the :py:func:`add_timeout` decorator.
    """
    pass


class _CallableContextManager(ABC, Generic[TCtx_]):
    debug: bool

    @abstractmethod
    def __enter__(self) -> TCtx_:
        ...

    @abstractmethod
    def __exit__(self, *a, **k) -> Any:
        ...

    def __call__(self, func: Callable[PS, T]) -> Callable[PS, T]:
        """
        Wrap ``func()`` so that its calls always happen in the context
        of the instance.
        """
        @wraps(func)
        def wrapper(*args: PS.args, **kwargs: PS.kwargs) -> T:
            with self:
                return func(*args, **kwargs)

        return wrapper

    def _debug(self, msg: str, **kwargs) -> None:
        if not self.debug:
            return
        header = f'{os.environ["PYTEST_CURRENT_TEST"]}: {type(self).__name__}'
        print(f'{header}: {msg}', **kwargs)


# ========================== Misc. functions ===========================


def strip(s: str) -> str:
    return dedent(s).strip('\n')


def import_target(target: str) -> Any:
    try:
        return import_module(target)
    except ImportError:  # Not a module
        assert '.' in target
        module, _, attr = target.rpartition('.')
        return getattr(import_module(module), attr)


def search_cache_logs(
    cache: LineProfilingCache,
    expecting_logs: bool,
    patterns: Mapping[str, bool] | Collection[str],
    match_individual_messages: bool = False,
    flags: int = 0,
) -> None:
    entries = cache._gather_debug_log_entries()
    ResultMismatch.compare(
        expecting_logs, bool(entries),
        expected='logs' if expecting_logs else 'no logs',
        actual=repr(entries) if entries else 'nothing',
    )
    if not expecting_logs:
        return
    text_chunks: list[str] = [entry.to_text() for entry in entries]
    if not match_individual_messages:
        text_chunks = ['\n'.join(text_chunks)]
    if isinstance(patterns, Mapping):
        to_match: dict[str, bool] = {
            str(pat): bool(should_match)
            for pat, should_match in patterns.items()
        }
    else:
        to_match = dict.fromkeys(patterns, True)
    for pat, should_match in to_match.items():
        pattern = re.compile(pat, flags)
        if any(pattern.search(chunk) for chunk in text_chunks) == should_match:
            continue
        raise ResultMismatch(
            f'pattern {pattern!r} to {"" if should_match else "not "}match '
            f'{cache!r}\'s logs: {text_chunks!r}'
        )


# ================ `pytest` stuff: fixtures and markers ================


@dataclasses.dataclass
class ModuleFixture:
    """
    Convenience wrapper around a Python source file which represents an
    importable module.

    Attributes:
        path (Path):
            File path to the module content.
        monkeypatch (pytest.MonkeyPatch):
            Monkey-patch object.
        dependencies (Collection[ModuleFixture[Any]]):
            Other :py:class:`ModuleFixture` objects this instance
            depends on.
        build_command_line \
(Callable[Concatenate[bool, StartMethod | None, ...], Sequence[str]] \
| None):
            If the module is supposed to be callable (via
            ``python -m ...``), a callable which takes the leading
            positional arguments
            ``fail: bool, start_method: Literal[...] | None`` followed by
            arbitrary arguments, and constructs the command-line
            arguments to pass to the module;
            note that:

            - ``fail=True`` should result in command-line arguments
              causing the executed module to fail.

            - ``start_method`` should select the
              :py:mod:`multiprocessing` start method, where ``dummy``
              should cause the analogous APIs at
              :py:mod:`multiprocessing.dummy` to be used instead;
              if it is :py:const:`None`, the OS default should be used.
        get_output (Callable[..., Any] | None):
            If the module is supposed to be callable (via
            ``python -m ...``), a callable which takes arbitrary
            arguments (consistent with those to
            ``build_command_line()``) and returns the expected output of
            the executed module.
    """
    path: Path
    monkeypatch: pytest.MonkeyPatch
    dependencies: Collection[ModuleFixture] = ()
    build_command_line: (
        Callable[Concatenate[bool, StartMethod | None, ...], Sequence[str]]
        | None
    ) = None
    get_output: Callable[..., Any] | None = None

    def install(
        self, *,
        local: bool = False, children: bool = False, deps_only: bool = False,
    ) -> None:
        """
        Set the module at :py:attr:`~.path` up to be importable.

        Args:
            local (bool):
                Make it importable for the CURRENT process (via
                :py:data:`sys.path`).
            children (bool):
                Make it importable for CHILD processes (via
                ``os.environ['PYTHONPATH']``).
            deps_only (bool):
                If true, only does the equivalent setup for
                dependencies.
        """
        for dep in self.dependencies:
            dep.install(local=local, children=children)
        if deps_only:
            return
        path = str(self.path.parent)
        if local:
            self.monkeypatch.syspath_prepend(path)
        if children:
            self.monkeypatch.setenv('PYTHONPATH', path, prepend=os.pathsep)

    def _import_module_helper(self) -> Generator[ModuleType, None, None]:
        def iter_module_names(
            module: ModuleFixture,
        ) -> Generator[str, None, None]:
            yield module.name
            for dep in module.dependencies:
                yield from iter_module_names(dep)

        self.install(local=True, children=True)
        try:
            yield import_module(self.name)
        finally:
            for name in set(iter_module_names(self)):
                sys.modules.pop(name, None)

    @staticmethod
    def propose_name(prefix: str) -> Generator[str, None, None]:
        """
        Propose a valid module name that isn't already occupied.
        """
        while True:
            name = '_'.join([prefix] + str(uuid4()).split('-'))
            if name not in sys.modules:
                assert name.isidentifier()
                yield name

    @property
    def name(self) -> str:
        return self.path.stem


@final
@dataclasses.dataclass
class Params:
    """
    Convenience wrapper around :py:func:`pytest.mark.parametrize`.
    """
    params: tuple[str, ...]
    values: list[tuple[Any, ...]]
    defaults: tuple[Any, ...]

    def __post_init__(self) -> None:
        n = len(self.params)
        assert all(p.isidentifier() for p in self.params)  # Validity
        assert len(set(self.params)) == n  # Uniqueness
        assert len(self.defaults) == n  # Consistency
        self.values = list(self._unique(self.values))
        assert all(len(v) == n for v in self.values)

    def __mul__(self, other: Self) -> Self:
        """
        Form a Cartesian product between the two instances with disjoint
        :py:attr:`~.params`, like stacking the
        :py:func:`pytest.mark.parametrize `decorators.

        Example:
            >>> p1 = Params.new(('a', 'b'), [(0, 0), (1, 2), (3, 4)],
            ...                 defaults=(1, 2))
            >>> p2 = Params.new('c', [0, 5, 6])
            >>> p1 * p2  # doctest: +NORMALIZE_WHITESPACE
            Params(params=('a', 'b', 'c'),
                   values=[(0, 0, 0), (0, 0, 5), (0, 0, 6),
                           (1, 2, 0), (1, 2, 5), (1, 2, 6),
                           (3, 4, 0), (3, 4, 5), (3, 4, 6)],
                   defaults=(1, 2, 0))
        """
        assert not set(self.params) & set(other.params)
        return type(self)(
            self.params + other.params,
            [sv + ov for sv in self.values for ov in other.values],
            self.defaults + other.defaults,
        )

    def __add__(self, other: Self) -> Self:
        """
        Concatenate two instances:

        - For parameters appearing in both, their lists of values are
          concatenated.

        - For parameters appearing in either instance, the missing
          values are taken from the other instance's
          :py:attr:`~.defaults`.

        Note:
            In the case of clashes, the :py:attr:`~.defaults` and the
            order of the :py:attr:`~.params` of ``self`` (the left
            operand) take precedence.

        Example:
            >>> p1 = Params.new(('a', 'b', 'c'),
            ...                 [(0, 0, 0),  # defaults
            ...                  (1, 2, 3), (4, 5, 6)])
            >>> p2 = Params.new(('c', 'd'), [(7, 8), (9, 10)],
            ...                 defaults=(-1, -1))
            >>> p1 + p2  # doctest: +NORMALIZE_WHITESPACE
            Params(params=('a', 'b', 'c', 'd'),
                   values=[(0, 0, 0, -1),
                           (1, 2, 3, -1),
                           (4, 5, 6, -1),
                           (0, 0, 7, 8),
                           (0, 0, 9, 10)],
                   defaults=(0, 0, 0, -1))
        """
        self_defaults = dict(zip(self.params, self.defaults))
        other_defaults = dict(zip(other.params, other.defaults))
        new_params = tuple(self._unique(self.params + other.params))

        defaults = {**other_defaults, **self_defaults}
        new_defaults_tuple = tuple(defaults[p] for p in new_params)

        new_values: list[tuple[Any, ...]] = []
        for old_values, old_params in [
            (self.values, self.params), (other.values, other.params),
        ]:
            indices: list[
                tuple[Literal[True], int] | tuple[Literal[False], str]
            ] = [
                (True, old_params.index(p)) if p in old_params else (False, p)
                for p in new_params
            ]
            new_values.extend(
                tuple(
                    (
                        value[cast(int, index)]
                        if available else
                        defaults[cast(str, index)]
                    ) for available, index in indices
                )
                for value in old_values
            )
        return type(self)(new_params, new_values, new_defaults_tuple)

    def sorted(
        self,
        *,
        sort_by: Sequence[str] | None = None,
        sortable_types: type[Any] | tuple[type[Any], ...] = (Real, str, bytes),
    ) -> Self:
        """
        Sort by parametrization values.

        Args:
            sort_by (Sequence[str] | None):
                Column names to sort by; default is to sort by all
                sortable params.
            sortable_types (type[Any] | tuple[type[Any], ...]):
                Type(s) where if a param has all its values being
                instances thereof (excl. :py:const:`None`s), said param
                is considered sortable.

        Returns:
            New instance
        """
        def sort_key(obj: Any) -> tuple[bool, str, Any]:
            type_name = '{0.__module__}.{0.__qualname__}'.format(type(obj))
            return (obj is None), type_name, obj

        if sort_by is None:
            sort_by = self.params
        sortable_columns: set[str] = {
            param for param, *values in zip(self.params, *self.values)
            if all(isinstance(v, sortable_types) or v is None for v in values)
        }
        sorted_column_indices: tuple[int, ...] = tuple(
            i for i, param in enumerate(sort_by) if param in sortable_columns
        )

        if sorted_column_indices:
            new_values = sorted(
                self.values, key=lambda vtuple: tuple(
                    sort_key(vtuple[i]) for i in sorted_column_indices
                ),
            )
        else:  # Fallback
            new_values = self.values.copy()
        return type(self)(self.params, new_values, self.defaults)

    def drop_params(self, params: Collection[str] | str) -> Self:
        """
        Return a new instance with the named ``params`` dropped; params
        that don't match :py:attr:`.params` are ignored.

        Example:
            >>> p = Params.new(('a', 'b'), [(1, 2), (3, 4)])
            >>> p.drop_params('a')
            Params(params=('b',), values=[(2,), (4,)], defaults=(2,))
            >>> assert p.drop_params(['c', 'd']) == p
        """
        def drop(t: tuple[T, ...]) -> tuple[T, ...]:
            return tuple(item for i, item in enumerate(t) if i not in dropped)

        if isinstance(params, str):
            params = params,
        dropped = {i for i, p in enumerate(self.params) if p in params}
        return type(self)(
            drop(self.params),
            [drop(pvalues) for pvalues in self.values],
            drop(self.defaults),
        )

    @overload
    def split_on_params(
        self, params: tuple[str, ...], *, drop_split_params: bool = True,
    ) -> dict[tuple[Any, ...], Self]:
        ...

    @overload
    def split_on_params(
        self, params: str, *, drop_split_params: bool = True,
    ) -> dict[Any, Self]:
        ...

    def split_on_params(
        self, params: tuple[str, ...] | str, *, drop_split_params: bool = True,
    ) -> dict[tuple[Any, ...], Self] | dict[Any, Self]:
        """
        Return new instances splitting on the values of the named
        ``params``; params that don't match :py:attr:`.params` results
        in an error.

        Example:
            >>> p = Params.new(('a', 'b', 'c'),
            ...                [(1, 2, True),
            ...                 (1, 2, False),
            ...                 (3, 4, True)])

            >>> p.split_on_params('a')  # doctest: +NORMALIZE_WHITESPACE
            {1: Params(params=('b', 'c'),
                       values=[(2, True), (2, False)],
                       defaults=(2, True)),
             3: Params(params=('b', 'c'),
                       values=[(4, True)],
                       defaults=(2, True))}

            >>> p.split_on_params(  # doctest: +NORMALIZE_WHITESPACE
            ...     ('a', 'b'),
            ... )
            {(1, 2): Params(params=('c',),
                            values=[(True,), (False,)],
                            defaults=(True,)),
             (3, 4): Params(params=('c',),
                            values=[(True,)],
                            defaults=(True,))}

            >>> p.split_on_params(  # doctest: +NORMALIZE_WHITESPACE
            ...     'a', drop_split_params=False,
            ... )
            {1: Params(params=('a', 'b', 'c'),
                       values=[(1, 2, True), (1, 2, False)],
                       defaults=(1, 2, True)),
             3: Params(params=('a', 'b', 'c'),
                       values=[(3, 4, True)],
                       defaults=(1, 2, True))}

            >>> p.split_on_params(  # doctest: +NORMALIZE_WHITESPACE
            ...     ('c', 'd'),
            ... )
            Traceback (most recent call last):
              ...
            ValueError: params = ('c', 'd'):
            these params not found: ['d']
        """
        if isinstance(params, str):
            params = params,
            unpack = True
        else:
            unpack = False
        nonexistent = sorted(set(params) - set(self.params))
        if nonexistent:
            raise ValueError(
                f'params = {params!r}: these params not found: {nonexistent!r}'
            )
        split_params: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
        indices = tuple(self.params.index(p) for i, p in enumerate(params))
        for pvalues in self.values:
            key = tuple(pvalues[i] for i in indices)
            split_params.setdefault(key, []).append(pvalues)
        new = partial(type(self), params=self.params, defaults=self.defaults)
        instances: dict[tuple[Any, ...], Self] = {
            key: new(values=values) for key, values in split_params.items()
        }
        if drop_split_params:
            instances = {
                key: instance.drop_params(params)
                for key, instance in instances.items()
            }
        if not unpack:
            return instances
        return {key[0]: instance for key, instance in instances.items()}

    def __call__(self, func: C) -> C:
        """
        Mark a callable as with :py:func:`pytest.mark.parametrize`.
        """
        # Note: `pytest` automatically assumes single-param values to
        # be unpacked, so comply here
        if len(self.params) == 1:
            marker = pytest.mark.parametrize(
                self.params[0], [v[0] for v in self.values],
            )
        else:
            marker = pytest.mark.parametrize(self.params, self.values)
        return marker(func)

    @staticmethod
    def _unique(items: Iterable[T]) -> Generator[T, None, None]:
        seen: set[T] = set()
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            yield item

    @overload
    @classmethod
    def new(
        cls,
        params: Sequence[str] | str,
        values: Sequence[Sequence[Any]],
        defaults: Sequence[Any] | _NotSupplied = NOT_SUPPLIED,
    ) -> Self:
        ...

    @overload
    @classmethod
    def new(
        cls,
        params: str,
        values: Sequence[Any],
        defaults: Any | _NotSupplied = NOT_SUPPLIED,
    ) -> Self:
        ...

    @classmethod
    def new(
        cls,
        params: Sequence[str] | str,
        values: Sequence[Sequence[Any]] | Sequence[Any],
        defaults: Sequence[Any] | Any | _NotSupplied = NOT_SUPPLIED,
    ) -> Self:
        """
        Instantiator more akin to :py:func:`pytest.mark.parametrize`:

        - ``params`` can be provided as a comma-separated string

        - Single parameters can be unpacked (singular param-name string
          and param-value sequences)

        - If ``defaults`` are not given, it is implicitly set to the
          FIRST item in ``values``.
        """
        if isinstance(params, str):
            param_list: tuple[str, ...] = tuple(
                p.strip() for p in params.split(',')
            )
            unpacked = len(param_list) == 1
        else:
            param_list = tuple(params)
            unpacked = False
        if defaults == NOT_SUPPLIED:
            defaults, *_ = values
        if unpacked:
            default_values: tuple[Any, ...] = defaults,
            value_tuple_list: list[tuple[Any, ...]] = [(v,) for v in values]
        else:
            default_values = tuple(defaults)  # type: ignore[arg-type]
            value_tuple_list = [tuple(v) for v in values]
        return cls(param_list, value_tuple_list, default_values)


# ============================= Failsafes ==============================


class preserve_object_attrs(_CallableContextManager[dict[str, Any]]):
    """
    Protect attributes on a single supplied object.
    """
    def __init__(
        self, obj: Any, attrs: Collection[str], *,
        static: bool = True, debug: bool = DEBUG,
    ) -> None:
        self.obj = obj
        self.attrs = set(attrs)
        self._callbacks: list[Callable[[], None]] = []
        self.debug = debug
        self.static = static

    def __enter__(self) -> dict[str, Any]:
        def get_repr(attr: str) -> str:
            try:
                value = get_attribute(self.obj, attr)
            except ValueError:
                return '<N/A>'
            else:
                return repr(value)

        def delete(attr: str) -> None:
            try:
                self._debug('Deleted attr `.{} = {}` on `{!r}`'.format(
                    attr, get_repr(attr), self.obj,
                ))
                delattr(self.obj, attr)
            except AttributeError:
                pass

        def reset(attr: str, value: Any) -> None:
            self._debug('Reset attr `.{} = {} -> {!r}` on `{!r}`'.format(
                attr, get_repr(attr), value, self.obj,
            ))
            setattr(self.obj, attr, value)

        if self.static:
            get_attribute: _GetAttr = inspect.getattr_static
        else:
            get_attribute = getattr

        result: dict[str, Any] = {}
        for attr in self.attrs:
            old = get_attribute(self.obj, attr, NOT_SUPPLIED)
            if old is NOT_SUPPLIED:
                callback = partial(delete, attr)
            else:
                callback = partial(reset, attr, old)
            result[attr] = old
            self._callbacks.append(callback)
        return result

    def __exit__(self, *_, **__) -> None:
        for callback in self._callbacks[::-1]:
            try:
                callback()
            except Exception:
                pass


class preserve_targets(_CallableContextManager[dict[str, dict[str, Any]]]):
    """
    Protect attributes on multiple target objects, which are resolved at
    context entry.

    Example:
        >>> from functools import wraps
        >>> from line_profiler.curated_profiling import (
        ...     CuratedProfilerContext,
        ... )
        >>> from line_profiler import line_profiler

        >>> assert not hasattr(CuratedProfilerContext, 'foo')
        >>> old_main = line_profiler.main
        >>>
        >>>
        >>> def foo(_) -> None:
        ...     pass
        ...
        >>>
        >>> @wraps(old_main)
        ... def main(*a, **k):
        ...     return old_main(*a, **k)
        ...
        >>>
        >>> preserved = {
        ...     'line_profiler.curated_profiling'
        ...     '.CuratedProfilerContext': {'foo'},
        ...     'line_profiler.line_profiler': {'main'},
        ... }
        >>> with preserve_targets(preserved, debug=False) as old:
        ...     assert old == {
        ...         'line_profiler.curated_profiling'
        ...         '.CuratedProfilerContext': {'foo': NOT_SUPPLIED},
        ...         'line_profiler.line_profiler': {'main': old_main},
        ...     }
        ...     CuratedProfilerContext.foo = foo
        ...     line_profiler.main = main
        ...     print('ok')
        ...
        ok
        >>> assert not hasattr(CuratedProfilerContext, 'foo')
        >>> assert old_main is \
old['line_profiler.line_profiler']['main']
        >>> assert old_main is line_profiler.main
        >>> assert main is not line_profiler.main
    """
    def __init__(
        self, targets: Mapping[str, Collection[str]] | None = None, *,
        static: bool = True, debug: bool = DEBUG,
    ) -> None:
        self.targets = targets
        self._stacks: list[ExitStack] = []
        self.static = static
        self.debug = debug

    def __enter__(self) -> dict[str, dict[str, Any]]:
        stack = ExitStack()
        self._stacks.append(stack)
        result: dict[str, Any] = {}
        for target, attrs in self.targets.items():
            result[target] = stack.enter_context(preserve_object_attrs(
                import_target(target), attrs,
                debug=self.debug, static=self.static,
            ))
        return result

    def __exit__(self, *_, **__) -> None:
        self._stacks.pop().close()

    @staticmethod
    def fetch_current_values(
        targets: Mapping[str, Collection[str]], static: bool = True,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        na = NOT_SUPPLIED
        if static:
            get: _GetAttr = inspect.getattr_static
        else:
            get = getattr
        for target, attrs in targets.items():
            obj = import_target(target)
            result[target] = {attr: get(obj, attr, na) for attr in attrs}
        return result

    @classmethod
    def compare_with_current_values(
        cls,
        old: Mapping[str, Mapping[str, Any]],
        comparator: Callable[[Any, Any], bool] = operator.is_,
        assert_true: bool | Mapping[str, Mapping[str, bool]] = True,
        static: bool = True,
    ) -> dict[str, dict[str, bool]]:
        def get_from_mapping(target: str, attr: str) -> bool:
            if TYPE_CHECKING:
                assert isinstance(assert_true, Mapping)
            return assert_true[target][attr]

        def get_from_boolean(*_, **__) -> bool:
            return True

        if isinstance(assert_true, Mapping):
            get_expected: Callable[[str, str], bool] = get_from_mapping
        else:
            get_expected = get_from_boolean

        result: dict[str, dict[str, bool]] = {}
        new = cls.fetch_current_values(old, static)
        failures: list[str] = []
        for target, old_values in old.items():
            new_values = new[target]
            cmp_results = result[target] = {}
            for attr, old_value in old_values.items():
                print(f'Checking: {target}.{attr}')
                new_value = new_values[attr]
                cmp_results[attr] = cmp_result = comparator(
                    new_value, old_value,
                )
                format_msg = partial(
                    '{}: {}'.format,
                    f'Compared `{target}.{attr}` '
                    f'(old: {old_value!r} @ {id(old_value):#x}; '
                    f'new: {new_value!r} @ {id(new_value):#x})',
                )
                expected_result = get_expected(target, attr)
                if assert_true:
                    if cmp_result == expected_result:
                        message = format_msg(
                            f'comparison result with {comparator!r} is '
                            f'{cmp_result} (as expected)'
                        )
                    else:
                        message = format_msg(
                            f'expected comparison with {comparator!r} to '
                            f'return {expected_result}, got {cmp_result}'
                        )
                        failures.append(message)
                else:
                    message = format_msg(
                        f'comparison result with {comparator!r}: {cmp_result}'
                    )
                print(message)
        assert (not failures), '\n'.join(failures)
        return result

    @property
    def targets(self) -> dict[str, set[str]]:
        if self._targets is None:
            # Defer resolution to context entrance
            self.targets = PATCH_SUMMARIES['maximal']
            assert self._targets is not None
        return self._targets

    @targets.setter
    def targets(self, targets: Mapping[str, Collection[str]] | None) -> None:
        if targets is None:
            self._targets = None
            return
        self._targets = {
            target: set(attrs) for target, attrs in targets.items()
        }


class cleanup_extra_pth_files(_CallableContextManager[frozenset[str]]):
    def __init__(self, debug: bool = DEBUG) -> None:
        self.debug = debug

    def __enter__(self) -> frozenset[str]:
        self.old = self.get_pth_files()
        return self.old

    def __exit__(self, *_, **__) -> None:
        for new_pth_file in self.get_pth_files() - self.old:
            self._debug(f'Deleting stray .pth file: {new_pth_file!r}')
            (self._get_path() / new_pth_file).unlink(missing_ok=True)
        del self.old

    @classmethod
    def get_pth_files(cls, name_only: bool = True) -> frozenset[str]:
        return frozenset(
            pth.name if name_only else str(pth)
            for pth in cls._get_path().glob('*.pth')
        )

    @staticmethod
    def _get_path() -> Path:
        return Path(sysconfig.get_path('purelib'))


def _cleanup_profiling_in_current_thread() -> None:
    """
    Disable all active profiler instances on the current thread, so that
    we don't trip over ourselves if a new thread down the road happens
    to reuse the thread ID.

    Note:
        The thread-local profiler state is supposed to be handled by
        :py:mod:`line_profiler._threading_patches` and
        :py:class:`.CuratedProfilerContext`, but since a test function
        decorated with :py:deco:`.add_timeout` will be isolated in a new
        thread BEFORE the fixture setting up such management
        (:py:func:`curated_profiler`) is invoked, we don't get that
        managing and will have to deal with profilers ourselves.
    """
    class Manager(Protocol):
        @property
        def active_instances(self) -> set[LineProfiler]:
            ...

    lp_managers = cast(
        dict[int, Manager], getattr(LineProfiler, '_managers', {}),
    )
    thread_id = threading.get_ident()
    if thread_id not in lp_managers:
        return
    instances = lp_managers[thread_id].active_instances
    for prof in set(instances):
        count = cast(int, getattr(prof, 'enable_count', 0))
        for _ in range(count):
            prof.disable_by_count()
        if count:
            print('Disabled', prof, 'in thread', hex(thread_id))
        # Removed after the last `.disable_by_count()`
        assert prof not in instances


@overload
def add_timeout(
    func: Callable[PS, T], *, timeout: float = DEFAULT_TIMEOUT,
) -> Callable[PS, T]:
    ...


@overload
def add_timeout(
    func: None = None, *, timeout: float = DEFAULT_TIMEOUT,
) -> Callable[[Callable[PS, T]], Callable[PS, T]]:
    ...


def add_timeout(
    func: Callable[PS, T] | None = None, *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Callable[PS, T] | Callable[[Callable[PS, T]], Callable[PS, T]]:
    """
    Decorate the test function so that it is run in another thread and
    can be timed out.

    Example:
        >>> from time import sleep

        >>> @add_timeout(timeout=.5)
        ... def my_func(
        ...     n: int, delay: float = 1, error: bool = False,
        ... ) -> list[int]:
        ...     sleep(delay)
        ...     if error:
        ...         raise RuntimeError('my error message')
        ...     return list(range(n))

        Normal execution:

        >>> my_func(3, 0) + [3]
        [0, 1, 2, 3]

        Erroring out:

        >>> my_func(3, 0, error=True)
        Traceback (most recent call last):
          ...
        RuntimeError: my error message

        Timing out:

        >>> my_func(4, delay=5)  # doctest: +NORMALIZE_WHITESPACE
        Traceback (most recent call last):
          ...
        test_child_procs.TestTimeout:
        my_func(4, delay=5): timed out after 0.5 s
    """
    if func is None:
        return cast(
            Callable[[Callable[PS, T]], Callable[PS, T]],
            partial(add_timeout, timeout=timeout),
        )

    @wraps(func)
    def worker(
        fobj: IO[bytes], /, *args: PS.args, **kwargs: PS.kwargs
    ) -> None:
        try:
            try:
                result = True, func(*args, **kwargs)
            except Exception as e:
                result = False, ExceptionHelper(e, e.__traceback__)
            # Do this instead of directly using `pickle.dump(..., fobj)`
            # so that pickling errors and file-handle-related errors are
            # handled separately
            serialized = pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)
            try:
                fobj.write(serialized)
                fobj.flush()
            except Exception:
                # Since this is run in a daemon thread, by the time this
                # write happens the main thread could've already timed
                # out and destroyed `fobj`... in that case just
                # gracefully exit
                pass
        finally:
            # See the docstring for that function to see why this is
            # needed.
            _cleanup_profiling_in_current_thread()

    @wraps(func)
    def wrapper(*args: PS.args, **kwargs: PS.kwargs) -> T:
        with BytesIO() as bio:
            thread = new_thread(args=(bio, *args), kwargs=kwargs)
            thread.start()
            thread.join(timeout)
            if not thread.is_alive():
                successful, result = pickle.loads(bio.getvalue())
                if successful:
                    return result
                assert isinstance(result, Exception)
                raise result
        args_repr = [repr(a) for a in args]
        args_repr.extend(f'{k}={v!r}' for k, v in kwargs.items())
        name = getattr(func, '__name__', repr(func))
        call_repr = f'{name}({", ".join(args_repr)})'
        msg = f'{call_repr}: timed out after {timeout:.2g} s'
        raise TestTimeout(msg)

    new_thread = partial(threading.Thread, target=worker, daemon=True)

    return wrapper


# =================== Warning handling/verification ====================


class _WarningInfo(Protocol):
    @property
    def message(self) -> str | Warning:
        ...

    @property
    def category(self) -> type[Warning]:
        ...

    @property
    def filename(self) -> str:
        ...

    @property
    def lineno(self) -> int:
        ...

    @property
    def line(self) -> str | None:
        ...


@dataclasses.dataclass
class _WarningMatcher:
    message: str | None = None
    category: type[Warning] | None = None
    module: str | None = None
    lineno: int | None = None
    _filters: dict[str, Callable[[Any], Any]] = dataclasses.field(
        repr=False, init=False, default_factory=dict,
    )

    def __post_init__(self) -> None:
        if self.message is not None:
            self._filters['message'] = partial(
                self._check_message, re.compile(self.message),
            )
        if self.category is not None:
            self._filters['category'] = partial(
                self._check_category, self.category,
            )
        if self.module is not None:
            self._filters['filename'] = partial(
                self._check_module, re.compile(self.module),
            )
        if self.lineno is not None:
            self._filters['lineno'] = partial(operator.eq, self.lineno)

    def __repr__(self) -> str:
        fields: dict[str, Any] = {
            field.name: getattr(self, field.name, None)
            for field in dataclasses.fields(self)
            if field.repr
        }
        return '{}({})'.format(
            type(self).__name__,
            ', '.join(
                f'{k}={v!r}' for k, v in fields.items() if v is not None
            ),
        )

    def match(self, info: _WarningInfo) -> bool:
        for field, check in self._filters.items():
            if not check(getattr(info, field)):
                return False
        return True

    @staticmethod
    def _check_message(
        msg_regex: re.Pattern, msg: str | Warning,
    ) -> re.Match | None:
        if not isinstance(msg, str):
            msg = str(msg)
        return msg_regex.match(msg)

    @staticmethod
    def _check_category(parent: type[Any], maybe_child: type[Any]) -> bool:
        try:
            return issubclass(maybe_child, parent)
        except Exception:
            return False

    @staticmethod
    def _check_module(
        module_regex: re.Pattern, filename: str,
    ) -> re.Match | None:
        module = modpath_to_modname(filename, hide_main=False, hide_init=False)
        return module_regex.match(module)


@dataclasses.dataclass
class _WarningContext:
    catch_warnings: warnings.catch_warnings = dataclasses.field(
        default_factory=partial(warnings.catch_warnings, record=True)
    )
    reissue_warnings: bool = False
    format_warnings: bool = False
    checks: list[tuple[_WarningMatcher, bool]] = dataclasses.field(
        default_factory=list,
    )
    filters: list[_WarningMatcher] = dataclasses.field(default_factory=list)
    _reissue_filtered_warnings: bool | None = dataclasses.field(
        init=False, default=None,
    )

    def forbid_warnings(self, *args, **kwargs) -> None:
        self.checks.append((self._get_matcher(*args, **kwargs), False))

    def expect_warnings(self, *args, **kwargs) -> None:
        self.checks.append((self._get_matcher(*args, **kwargs), True))

    def suppress_warnings(self, *args, **kwargs) -> None:
        if self._reissue_filtered_warnings is None:
            self._reissue_filtered_warnings = False
        elif self._reissue_filtered_warnings:
            raise RuntimeError(
                'Either `.suppress_warnings()` or `.propagate_warnings()` can '
                'be called within the same context, but not both'
            )
        self.filters.append(self._get_matcher(*args, **kwargs))

    def propagate_warnings(self, *args, **kwargs) -> None:
        if self._reissue_filtered_warnings is None:
            self._reissue_filtered_warnings = True
        elif not self._reissue_filtered_warnings:
            raise RuntimeError(
                'Either `.suppress_warnings()` or `.propagate_warnings()` can '
                'be called within the same context, but not both'
            )
        self.filters.append(self._get_matcher(*args, **kwargs))

    def check(self, infos: Sequence[_WarningInfo]) -> None:
        for matcher, allowed_or_required in self.checks:
            matches = [info for info in infos if matcher.match(info)]
            if matches and not allowed_or_required:
                if self.format_warnings:
                    matches_repr = (
                        f'{len(matches)}:\n{self._format_warnings(matches)}'
                    )
                else:
                    matches_repr = f'{len(matches)} ({matches!r})'
                raise ResultMismatch(
                    expected=f'no warnings matching {matcher!r}',
                    actual=matches_repr,
                )
            if not matches and allowed_or_required:
                warnings_repr = f'none out of {len(infos)}'
                if infos:
                    if self.format_warnings:
                        warnings_repr = (
                            f'{warnings_repr}:\n{self._format_warnings(infos)}'
                        )
                    else:
                        warnings_repr = f'{warnings_repr} ({infos!r})'
                raise ResultMismatch(
                    expected=f'warnings matching {matcher!r}',
                    actual=warnings_repr,
                )

    def reissue(self, infos: Sequence[_WarningInfo]) -> None:
        def reissue_(info: _WarningInfo) -> None:
            warnings.warn_explicit(
                message=info.message,
                category=info.category,
                filename=info.filename,
                lineno=info.lineno,
            )

        # If we haven't called `.suppress_warnings()` or
        # `.propagate_warnings()`, handle warnings according to the
        # default behavior
        if not self.filters:
            if self.reissue_warnings:
                for info in infos:
                    reissue_(info)
            return
        # Otherwise we handle the warnings matching any of the filters
        # one way, and the remaining the other way:
        # - If we used `.suppress_warnings()`, matching warnings are
        #   suppressed, and the remainder propagated by default
        # - If we used `.propagate_warnings()`, matching warnings are
        #   propagated, and the remainder suppressed by default
        # Note that we are not supposed to have called both methods
        reissue_by_default = not self._reissue_filtered_warnings
        for info in infos:
            should_reissue = reissue_by_default
            if any(matcher.match(info) for matcher in self.filters):
                should_reissue = not should_reissue
            if should_reissue:
                reissue_(info)

    @staticmethod
    def _format_warnings(infos: Sequence[_WarningInfo]) -> str:
        if not infos:
            return '<no warnings>'
        chunks: list[str] = []
        for info in infos:
            text = strip(warnings.formatwarning(
                message=info.message,
                category=info.category,
                filename=info.filename,
                lineno=info.lineno,
            ))
            chunks.append(f'- {info!r}')
            chunks.append(indent(text, '  '))
        return '\n'.join(chunks)

    @staticmethod
    def _get_matcher(
        message: str | None = None,
        category: type[Warning] | None = Warning,
        module: str | None = None,
        lineno: int | None = None,
    ) -> _WarningMatcher:
        return _WarningMatcher(
            message=message, category=category, module=module, lineno=lineno,
        )

    @classmethod
    def new(
        cls, /, *,
        reissue_warnings: bool = False,
        format_warnings: bool = False,
        **kwargs
    ) -> Self:
        kwargs['record'] = True
        return cls(
            warnings.catch_warnings(**kwargs),
            reissue_warnings=reissue_warnings,
            format_warnings=format_warnings,
        )


class CheckWarnings(Sequence[_WarningInfo]):
    """
    Helper context for deferring the checking of warnings to until
    context exit.

    Example:
        >>> import warnings

        >>> cw = CheckWarnings(
        ...     reissue_warnings=False, format_warnings=False,
        ... )

        >>> with cw:  # doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        ...     cw.forbid_warnings('foo', UserWarning)
        ...     warnings.warn('foobar')
        ...     print('This is printed before the error')
        ...
        This is printed before the error
        Traceback (most recent call last):
          ...
        test_child_procs.ResultMismatch: expected no warnings matching
        _WarningMatcher(message='foo',
                        category=<class 'UserWarning'>),
        got 1 ([...])

        >>> with cw:  # doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        ...     cw.expect_warnings(category=UserWarning)
        ...     warnings.warn('foobar', Warning)
        ...     print('This is printed before the error')
        ...
        This is printed before the error
        Traceback (most recent call last):
          ...
        test_child_procs.ResultMismatch: expected warnings matching
        _WarningMatcher(category=<class 'UserWarning'>),
        got none out of 1 ([...])
        >>> assert len(cw) == 1
        >>> assert str(cw[0].message) == 'foobar'
    """
    def __init__(
        self, /, *,
        reissue_warnings: bool = True,
        format_warnings: bool = True,
        **kwargs
    ) -> None:
        # Note: even with `reissue_warnings=True`, it is not guaranteed
        # that all the recorded warnings become visible, depending on
        # the warning filters in place before entering the context; so
        # we also use `format_warnings=True` by default to include the
        # warnings in the error message (if any)
        self._new_context: Callable[[], _WarningContext] = partial(
            _WarningContext.new,
            reissue_warnings=reissue_warnings,
            format_warnings=format_warnings,
            **kwargs,
        )
        self._contexts: list[
            tuple[_WarningContext, Sequence[_WarningInfo]]
        ] = []
        self._last_captured: Sequence[_WarningInfo] = []

    def forbid_warnings(self, *args, **kwargs) -> None:
        """
        Equivalent to calling
        ``filterwarnings('error', *args, **kwargs)``;
        at context exit, if ANY matching warning has been captured, an
        error will be raised.
        """
        ctx, _ = self._current_context
        ctx.forbid_warnings(*args, **kwargs)

    def expect_warnings(self, *args, **kwargs) -> None:
        """
        Equivalent to calling
        ``filterwarnings('always', *args, **kwargs)``;
        at context exit, if NO matching warnings have been captured, an
        error will be raised.
        """
        ctx, _ = self._current_context
        ctx.expect_warnings(*args, **kwargs)

    def suppress_warnings(self, *args, **kwargs) -> None:
        """
        Equivalent to calling
        ``filterwarnings('never', *args, **kwargs)``;
        at context exit, captured warnings are SUPPRESSED if they match
        the filter(s) and REISSUED otherwise.

        Note:
            Within each context level, one can call EITHER
            :py:meth:`.suppress_warnings` or
            :py:meth:`.propagate_warnings` but not both.
        """
        ctx, _ = self._current_context
        ctx.suppress_warnings(*args, **kwargs)

    def propagate_warnings(self, *args, **kwargs) -> None:
        """
        Equivalent to calling
        ``filterwarnings('always', *args, **kwargs)``;
        at context exit, captured warnings are REISSUED if they match
        the filter(s) and SUPPRESSED otherwise.

        Note:
            Within each context level, one can call EITHER
            :py:meth:`.suppress_warnings` or
            :py:meth:`.propagate_warnings` but not both.
        """
        ctx, _ = self._current_context
        ctx.propagate_warnings(*args, **kwargs)

    def __enter__(self) -> Self:
        ctx = self._new_context()
        infos: Sequence[_WarningInfo] | None
        infos = ctx.catch_warnings.__enter__()
        assert infos is not None
        self._contexts.append((ctx, infos))
        return self

    def __exit__(self, *args, **kwargs) -> None:
        ctx, infos = self._contexts.pop()
        try:
            ctx.check(infos)
        finally:
            self._last_captured = infos
            ctx.catch_warnings.__exit__(*args, **kwargs)
            # Now that the warning filters are reset we can reissue
            # the warnings without interference
            ctx.reissue(infos)

    @overload
    def __getitem__(self, i: int, /) -> _WarningInfo:
        ...

    @overload
    def __getitem__(self, i: slice, /) -> list[_WarningInfo]:
        ...

    def __getitem__(
        self, i: int | slice, /,
    ) -> _WarningInfo | Sequence[_WarningInfo]:
        return self._current_warnings[i]

    def __len__(self) -> int:
        return len(self._current_warnings)

    def __iter__(self) -> Iterator[_WarningInfo]:
        return iter(self._current_warnings)

    def __reversed__(self) -> Iterator[_WarningInfo]:
        return iter(reversed(self._current_warnings))

    def __contains__(self, item: Any, /) -> bool:
        return item in self._current_warnings

    def index(self, *args, **kwargs) -> int:
        return self._current_warnings.index(*args, **kwargs)

    def count(self, *args, **kwargs) -> int:
        return self._current_warnings.count(*args, **kwargs)

    @property
    def _current_context(
        self,
    ) -> tuple[_WarningContext, Sequence[_WarningInfo]]:
        return self._contexts[-1]

    @property
    def _current_warnings(self) -> Sequence[_WarningInfo]:
        try:
            return self._current_context[1]
        except IndexError:
            # Outside of contexts, just provide the last captured values
            # for convenience
            return self._last_captured


# ================= `multiprocessing` patch resolution =================

_PatchSummary = Mapping[str, Set[str]]

mp_patch_is_internal: Callable[[str], bool]
mp_patch_is_internal = operator.methodcaller('startswith', '__')


def get_patched_attributes(
    applied_mp_patches: Collection[str] | None = None,
) -> MappingProxyType[str, frozenset[str]]:
    if applied_mp_patches is None:
        applied_mp_patches = {
            patch for patch, applied in (
                ConfigSource.from_default()
                .get_subconfig('child_processes', 'multiprocessing', 'patches')
                .conf_dict.items()
            ) if applied
        }
    return _get_patched_attributes(frozenset(applied_mp_patches))


@lru_cache()
def _get_patched_attributes(
    applied_mp_patches: frozenset[str],
) -> MappingProxyType[str, frozenset[str]]:
    # Get the contents of the individual patches
    patches = PATCH_SUMMARIES['minimal'].copy()
    iter_summaries = (
        _MP_PATCHES[patch].summary
        for patch in applied_mp_patches if patch in _MP_PATCHES
    )
    patches = _get_patch_summary_union(patches, *iter_summaries)
    return MappingProxyType({
        target: frozenset(attrs)
        for target, attrs in filter_mp_patch_summary(patches).items()
    })


def get_mp_patches_toml_text(mp_patches: Collection[str]) -> str:
    """
    Return the TOML section configuring whether to apply individual
    :py:mod:`multiprocessing` patches.
    """
    mp_patches_as_dict = {
        name: name in mp_patches for name in _MP_PATCHES
        if not mp_patch_is_internal(name)
    }
    return (
        '[tool.line_profiler.child_processes.multiprocessing.patches]\n'
        + '\n'.join(
            f'{patch} = {str(applied).lower()}'
            for patch, applied in mp_patches_as_dict.items()
        )
    )


def _get_patch_summary_union(
    *summaries: _PatchSummary,
) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for summary in summaries:
        for target, attrs in summary.items():
            result[target] = result.get(target, frozenset()) | frozenset(attrs)
    return result


def summarize_mp_patches(
    summaries: Collection[tuple[bool, _PatchSummary]]
) -> dict[str, dict[str, bool]]:
    """
    Take individual patch summaries and whether each of them is applied,
    and return a dictionary representing whether each of the patch
    targets is to be patched.

    Example:
        >>> summarize_mp_patches([(False, {'foo': {'bar'}})])
        {'foo': {'bar': False}}
        >>> summarize_mp_patches([  # doctest: +NORMALIZE_WHITESPACE
        ...     (False, {'foo': {'bar', 'baz'}}),
        ...     (True, {'foo': {'baz', 'foobar'}, 'spam': {'ham'}}),
        ...     (False, {'foo': {'baz'}, 'spam': {'eggs'}})
        ... ])
        {'foo': {'bar': False, 'baz': True, 'foobar': True},
         'spam': {'eggs': False, 'ham': True}}
    """
    def get_all_mentioned(s: Iterable[_PatchSummary]) -> dict[str, set[str]]:
        all_items: dict[str, set[str]] = {}
        for summary in s:
            for target, attrs in summary.items():
                all_items.setdefault(target, set()).update(attrs)
        return all_items

    all_items = get_all_mentioned(s for _, s in summaries)
    all_patched = get_all_mentioned(s for applied, s in summaries if applied)
    result: dict[str, dict[str, bool]] = {
        target:
        {attr: attr in all_patched.get(target, set()) for attr in attrs}
        for target, attrs in all_items.items()
    }
    # Normalize the order for convenience
    return {
        target: dict(sorted(attrs.items()))
        for target, attrs in sorted(result.items())
    }


def filter_mp_patch_summary(summary: _PatchSummary) -> dict[str, set[str]]:
    """
    Filter the content of a
    :py:attr:`line_profiler._child_process_profiling.multiprocessing\
._infrastructure.Patch.summary`
    so that only patch targets that actually exists remains.
    """
    result: dict[str, set[str]] = {}
    for target, attrs in summary.items():
        try:
            obj = import_target(target)
        except ImportError:
            continue
        present_attrs = {a for a in attrs if hasattr(obj, a)}
        # Drop if none of the attributes is present
        if present_attrs:
            result[target] = present_attrs
    return result


PATCH_SUMMARIES: dict[
    Literal['minimal', 'maximal', 'default', 'pth_hook'],
    dict[str, frozenset[str]]
] = {
    'minimal': {'multiprocessing': frozenset({MP_PATCHED_MARKER})},
}
# Get patches that are dynamically resolved: while these patches are
# always applied, some of the patch targets are
# platform-/Pyhon-version-specific and may not always exist
_dynamically_resolved_patch_summaries: Iterable[_PatchSummary] = (
    patch.summary for name, patch in _MP_PATCHES.items()
    # Basic `multiprocessing` patches are always applied
    if mp_patch_is_internal(name)
)
_dynamically_resolved_patch_summaries = itertools.chain(
    _dynamically_resolved_patch_summaries,
    # some platforms e.g. Windows don't have `fork()`
    [{'os': frozenset({'fork'})}],
)
_dynamically_resolved_patch_summaries = cast(  # See `ty` issue #3428
    Iterable[_PatchSummary],
    map(filter_mp_patch_summary, _dynamically_resolved_patch_summaries),
)
PATCH_SUMMARIES['minimal'] = _get_patch_summary_union(
    PATCH_SUMMARIES['minimal'], *_dynamically_resolved_patch_summaries,
)

# This is only patched if we called
# `_line_profiler_hooks.load_pth_hook()`
PATCH_SUMMARIES['pth_hook'] = {
    f'{load_pth_hook.__module__}.{load_pth_hook.__qualname__}':
        frozenset({'called'}),
}
# Upper limit of what we could've patched
PATCH_SUMMARIES['maximal'] = _get_patch_summary_union(
    PATCH_SUMMARIES['minimal'],
    PATCH_SUMMARIES['pth_hook'],
    get_patched_attributes([
        name for name in _MP_PATCHES if not mp_patch_is_internal(name)
    ]),
)
# Actual patches using the default config
PATCH_SUMMARIES['default'] = _get_patch_summary_union(
    PATCH_SUMMARIES['minimal'], get_patched_attributes(),
)

# ========================= Command execution ==========================

# `shlex.join()` doesn't work properly on Windows, so use
# `subprocess.list2cmdline()` instead;
# though an "intentionally" undocumented API (cpython issue #10308),
# it's been around since 2.4, seems stable enough, and does exactly what
# is needed
concat_command_line: Callable[[Sequence[str]], str]
if sys.platform == 'win32':
    concat_command_line = subprocess.list2cmdline
else:
    concat_command_line = shlex.join


def run_subproc(
    cmd: Sequence[str] | str, /, *args, **kwargs
) -> subprocess.CompletedProcess:
    """
    Wrapper around :py:func:`subprocess.run` which writes debugging
    output.
    """
    class HasStreams(Protocol):
        @property
        def stdout(self) -> str | bytes | None:
            ...

        @property
        def stderr(self) -> str | bytes | None:
            ...

    if isinstance(cmd, str):
        cmd_str = cmd
    else:
        cmd_str = concat_command_line(cmd)

    print('Command:', cmd_str)
    _print_env_deltas(kwargs.get('env'))
    print('-- Process start --')
    # Note: somehow `mypy` doesn't agree with simply unpacking the
    # `*args` into `subprocess.run()`...
    status: int | str = '???'
    result: HasStreams | None = None
    subproc_errors = (
        subprocess.CalledProcessError, subprocess.TimeoutExpired,
    )
    time = monotonic()
    try:
        proc = subprocess.run(cmd, *args, **kwargs)
    except Exception as e:
        status = 'error'
        if isinstance(e, subproc_errors):
            result = e
        if hasattr(e, 'returncode'):  # `CalledProcessError`
            status = f'{status} ({e.returncode})'
        raise
    else:
        result, status = proc, proc.returncode
        return proc
    finally:
        time = monotonic() - time
        if result is not None:
            captured: str | bytes | None
            for name, captured, stream in [
                ('stdout', result.stdout, sys.stdout),
                ('stderr', result.stderr, sys.stderr),
            ]:
                if captured is None:
                    continue
                if isinstance(captured, bytes):  # `text=False`
                    captured = captured.decode()
                print(f'{name}:\n{indent(captured, "  ")}', file=stream)
        print(
            f'-- Process end (time elapsed: {time:.2f} s / '
            f'return status: {status}) --'
        )


# ======================== `kernprof` execution ========================


def _run_as_script(
    request: pytest.FixtureRequest,
    runner_args: list[str],
    test_args: list[str],
    test_module: ModuleFixture,
    *,
    subproc: bool = True,
    check_warnings: bool = True,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + [str(test_module.path)] + test_args
    if subproc:
        run: Callable[..., subprocess.CompletedProcess] = run_subproc
    else:
        run = partial(_run_kernprof_main_in_process, request, check_warnings)
    test_module.install(children=True, local=not subproc, deps_only=True)
    return run(cmd, **kwargs)


def _run_as_module(
    request: pytest.FixtureRequest,
    runner_args: list[str],
    test_args: list[str],
    test_module: ModuleFixture,
    *,
    subproc: bool = True,
    check_warnings: bool = True,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + ['-m', test_module.name] + test_args
    if subproc:
        run: Callable[..., subprocess.CompletedProcess] = run_subproc
    else:
        run = partial(_run_kernprof_main_in_process, request, check_warnings)
    test_module.install(children=True, local=not subproc)
    return run(cmd, **kwargs)


def _run_as_literal_code(
    request: pytest.FixtureRequest,
    runner_args: list[str],
    test_args: list[str],
    test_module: ModuleFixture,
    *,
    subproc: bool = True,
    check_warnings: bool = True,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + ['-c', test_module.path.read_text()] + test_args
    if subproc:
        run: Callable[..., subprocess.CompletedProcess] = run_subproc
    else:
        run = partial(_run_kernprof_main_in_process, request, check_warnings)
    test_module.install(children=True, local=not subproc, deps_only=True)
    return run(cmd, **kwargs)


@cleanup_extra_pth_files()
@preserve_targets()
def _run_kernprof_main_in_process(
    request: pytest.FixtureRequest, check_warnings: bool, cmd: Sequence[str],
    *,
    text: bool = False,
    capture_output: bool = False,
    check: bool = False,
    encoding: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    **_kwargs
) -> subprocess.CompletedProcess:
    """
    Emulate running :cmd:`kernprof` in a subprocess with in-process
    machineries as best as we can, so that we can retrieve more
    debugging output when things do go south.
    """
    def get_streams(
    ) -> tuple[str, str] | tuple[bytes, bytes] | tuple[None, None]:
        if cap is None:
            return None, None
        stdout, stderr = cap.readouterr()
        if text:
            return stdout, stderr
        if encoding is None:
            encode = operator.methodcaller('encode')
        else:
            encode = operator.methodcaller('encode', encoding)
        return encode(stdout), encode(stderr)

    assert not _kwargs
    assert cmd[0] == 'kernprof'

    cap: pytest.CaptureFixture | None = None
    stdout: str | bytes | None = None
    stderr: str | bytes | None = None
    if capture_output:
        cap = request.getfixturevalue('capsys')
    print('Command:', concat_command_line(cmd))
    _print_env_deltas(env)
    print('-- Emulated process start --')
    get_streams()  # Don't include the above in the captured outputs

    status: int | str = '???'
    timed_out = False
    time = monotonic()
    if timeout:
        main = add_timeout(kernprof_main, timeout=timeout)
    else:
        main = kernprof_main
    with ExitStack() as stack:
        if check_warnings:
            # See similar indictions against warnings in
            # `_test_apply_mp_patches()`
            cw = stack.enter_context(CheckWarnings())
            cw.forbid_warnings('.*resource_tracker', module='multiprocessing')
            cw.forbid_warnings(
                r'.* file\(s\) .* empty',
                category=UserWarning, module='line_profiler',
            )
            if timeout:
                cw.suppress_warnings(
                    r'.*multi-?threaded.*fork\(\)',
                    category=DeprecationWarning,
                )
        try:
            try:
                cleanup = stack.enter_context(Cleanup())
                if env is not None:
                    cleanup.update_mapping(os.environ, env)
                main(cmd[1:], exit_on_error=False)
            except TestTimeout:  # `subprocess` uses `SIGKILL`
                returncode, timed_out = -9, True
                status = f'error ({returncode})'
            except Exception as e:
                # Format and output the tracebacks, otherwise we would've
                # suppressed them
                traceback.print_exception(e)
                returncode = 2 if isinstance(e, ArgumentError) else 1
                status = f'error ({returncode})'
            else:
                status = returncode = 0
            stdout, stderr = get_streams()

            # Turn in-process errors into the corresponding `subprocess`
            # errors
            if timed_out:
                assert timeout is not None  # Assure the typechecker
                raise subprocess.TimeoutExpired(cmd, timeout, stdout, stderr)
            if check and returncode:
                raise subprocess.CalledProcessError(
                    returncode, cmd, stdout, stderr,
                )
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
        finally:
            time = monotonic() - time
            captured: str | bytes | None
            for name, captured, stream in [
                ('stdout', stdout, sys.stdout), ('stderr', stderr, sys.stderr),
            ]:
                if captured is None:
                    continue
                if isinstance(captured, bytes):  # `text=False`
                    captured = captured.decode()
                print(f'{name}:\n{indent(captured, "  ")}', file=stream)
            print(
                f'-- Emulated process end (time elapsed: {time:.2f} s / '
                f'return status: {status}) --'
            )


def _print_env_deltas(env: Mapping[str, str] | None = None) -> None:
    if env is None:
        return
    diff: list[str] = []
    for key in set(os.environ).union(env):
        old = os.environ.get(key)
        new = env.get(key)
        if old is not None is new:
            item = f'{old!r} -> (deleted)'
        elif old is None is not new:
            item = f'{new!r} (added)'
        else:
            if old == new:
                continue
            item = f'{old!r} -> {new!r}'
        diff.append(f'${{{key}}}: {item}')
    if diff:
        print('Env:', indent('\n'.join(diff), '  '), sep='\n')


@cleanup_extra_pth_files()
def _run_test_module(
    run_helper: Callable[..., subprocess.CompletedProcess],
    request: pytest.FixtureRequest,
    test_module: ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    runner: str | list[str] = 'kernprof',
    outfile: str | None = None,
    profile: bool = True,
    *,
    profiled_code_is_tempfile: bool = False,
    fail: bool = False,
    start_method: StartMethod | None = None,
    test_module_args: Sequence[Any] | None = None,
    test_module_kwargs: Mapping[str, Any] | None = None,
    check: bool = True,
    debug_log: str | None = None,
    nhits: Mapping[str, int] | None = None,
    subproc: bool = True,
    **kwargs
) -> tuple[subprocess.CompletedProcess, LineStats | None]:
    """
    Returns:
        process_running_the_test_module (subprocess.CompletedProcess):
            Process object
        profliing_stats (LineStats | None):
            Line-profiling stats (where available)
    """
    if isinstance(runner, str):
        runner_cli_args: list[str] = [runner]
    else:
        runner_cli_args = list(runner)

    if not profile:
        nhits = None

    if profile and not profiled_code_is_tempfile:
        runner_cli_args.extend(['--prof-mod', str(test_module.path)])
    if nhits is not None:
        # We need `kernprof` to write the profliing results immediately
        # to preserve data from tempfiles (see note below)
        runner_cli_args.append('--view')

    if start_method and start_method not in ('dummy', *START_METHODS):
        pytest.skip(
            f'`multiprocessing` start method {start_method!r} '
            'not available on the platform'
        )
    if test_module_args is None:
        test_module_args = ()
    if test_module_kwargs is None:
        test_module_kwargs = {}
    assert test_module.build_command_line
    assert test_module.get_output
    test_cli_args = test_module.build_command_line(
        fail, start_method, *test_module_args, **test_module_kwargs,
    )

    with ub.ChDir(tmp_path_factory.mktemp('mytemp')):
        if outfile is not None:
            runner_cli_args.extend(['--outfile', outfile])
        if debug_log:
            runner_cli_args.extend(['--debug-log', debug_log])
        old_pth_files = cleanup_extra_pth_files.get_pth_files()
        try:
            proc = run_helper(
                request, runner_cli_args, test_cli_args, test_module,
                text=True, capture_output=True, check=(check and not fail),
                subproc=subproc,
                **kwargs
            )
            # Checks:
            if fail:
                # - The process has failed as expected
                if check:
                    assert proc.returncode
            else:
                # - The result is correctly calculated
                expected = test_module.get_output(
                    *test_module_args, **test_module_kwargs,
                )
                output_lines = proc.stdout.splitlines()
                ResultMismatch.compare(str(expected), output_lines[0])
            # - Temporary `.pth` file(s) created by
            # `LineProfilingCache.write_pth_hook()` has been cleaned up
            assert cleanup_extra_pth_files.get_pth_files() == old_pth_files
            # - Profiling results are written to the specified file
            prof_result: LineStats | None = None
            if outfile is None:
                assert not list(Path.cwd().iterdir())
            else:
                assert os.path.exists(outfile)
                assert os.stat(outfile).st_size
                if profile:
                    prof_result = LineStats.from_files(outfile)
            # - If we're keeping track, the function is called the
            #   expected number of times and has run the expected # of
            #   loops (Note: we do it by parsing the output of
            #   `kernprof -v` instead of reading the `--outfile`,
            #   because if the profiled code is in a tempfile the
            #   profiling data will be dropped in the written outfile)
            for tag, num in (nhits or {}).items():
                check_tagged_line_nhits(proc.stdout, tag, num)
        except subprocess.TimeoutExpired as e:
            # If we're not in running in a subproc, while
            # `kernprof.main()` shouldn't have gotten around to write
            # the debug logs, we MIGHT be able to retrieve them from the
            # active `LineProfilingCache` instance...
            try:
                if not subproc:
                    cache = LineProfilingCache.load()
                    if debug_log is None:
                        alt_debug_log = 'debug.log'
                    else:
                        alt_debug_log = debug_log + '.alt'
                    with open(alt_debug_log, mode='w') as fobj:
                        for entry in cache._gather_debug_log_entries():
                            print(entry.to_text(), file=fobj)
                    debug_log = alt_debug_log
            except Exception:
                pass
            finally:
                raise e
        finally:
            if debug_log is not None and os.path.exists(debug_log):
                with open(debug_log) as fobj:
                    print('-- Combined debug logs --', file=sys.stderr)
                    print(indent(fobj.read(), '  '), end='', file=sys.stderr)
                    print('-- End of debug logs --', file=sys.stderr)
    return proc, prof_result


def check_tagged_line_nhits(output: str, tag: str, nhits: int) -> None:
    """
    Check the output of :py:meth:`LineStats.print` for the number of
    hits on the line tagged with the comment ``# GREP_MARKER[<...>]``.
    """
    # The line should be preixed with 5 numbers:
    # lineno, nhits, time, time-per-hit, % time
    actual_nhits = 0
    for line in output.splitlines():
        if line.endswith(f'# GREP_MARKER[{tag}]'):
            try:
                _, n, _, _, _, *_ = line.split()
                actual_nhits += int(n)
            except Exception:
                pass
    ResultMismatch.compare(
        nhits, actual_nhits,
        expected=f'{nhits} hit(s) on line(s) tagged with {tag!r}',
    )


run_module = partial(_run_test_module, _run_as_module)
run_script = partial(_run_test_module, _run_as_script)
run_literal_code = partial(
    _run_test_module, _run_as_literal_code, profiled_code_is_tempfile=True,
)
