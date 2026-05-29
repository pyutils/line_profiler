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
    Callable,
    Collection, Generator, Iterable, Iterator, Mapping, Sequence, Set,
)
from contextlib import ExitStack
from functools import lru_cache, partial, wraps
from io import BytesIO, StringIO
from importlib import import_module
from multiprocessing.pool import (  # type: ignore
    ExceptionWithTraceback as ExceptionHelper,
)
from numbers import Real
from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory
from textwrap import dedent, indent
from time import monotonic
from types import MappingProxyType, ModuleType, TracebackType
from typing import (
    TYPE_CHECKING, Any, Generic, IO, Literal, Protocol, TypeVar,
    cast, final, overload,
)
from typing_extensions import Self, ParamSpec
from uuid import uuid4

import pytest
import ubelt as ub

from _line_profiler_hooks import load_pth_hook
from kernprof import main as kernprof_main
from line_profiler._child_process_profiling.cache import LineProfilingCache
from line_profiler._child_process_profiling.runpy_patches import (
    create_runpy_wrapper,
)
from line_profiler._child_process_profiling.multiprocessing_patches import (
    _Poller, MPConfig, _PATCHED_MARKER, _PATCHES as MP_PATCHES,
)
from line_profiler.autoprofile.util_static import modpath_to_modname
from line_profiler.cleanup import Cleanup
from line_profiler.curated_profiling import (
    CuratedProfilerContext, ClassifiedPreimportTargets,
)
from line_profiler.line_profiler import LineProfiler, LineStats
from line_profiler.toml_config import ConfigSource


T = TypeVar('T')
T1 = TypeVar('T1')
T2 = TypeVar('T2')
TCtx_ = TypeVar('TCtx_')
PS = ParamSpec('PS')
C = TypeVar('C', bound=Callable[..., Any])

NUM_NUMBERS = 100
NUM_PROCS = 4
START_METHODS = set(multiprocessing.get_all_start_methods())

_TEST_TIMEOUT = 5  # Seconds
_DEBUG = True
_WINDOWS = sys.platform == 'win32'


def strip(s: str) -> str:
    return dedent(s).strip('\n')


EXTERNAL_MODULE_BODY = strip("""
from __future__ import annotations


def my_external_sum(x: list[int], fail: bool = False) -> int:
    result: int = 0  # GREP_MARKER[EXT-INVOCATION]
    for item in x:
        result += item  # GREP_MARKER[EXT-LOOP]
    if fail:
        raise RuntimeError('forced failure')
    return result
""")

TEST_MODULE_TEMPLATE = strip("""
from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Callable
from multiprocessing import dummy, get_context, Pool
from typing import Literal

from {EXT_MODULE} import my_external_sum


def my_local_sum(x: list[int], fail: bool = False) -> int:
    result: int = 0  # GREP_MARKER[LOCAL-INVOCATION]
    # The reversing is to prevent bytecode aliasing with
    # `my_external_sum()` (see issue #424, PR #425)
    for item in reversed(x):
        result += item  # GREP_MARKER[LOCAL-LOOP]
    if fail:
        raise RuntimeError('forced failure')
    return result


def sum_in_child_procs(
    length: int, n: int, my_sum: Callable[[list[int]], int],
    start_method: Literal[
        'fork', 'forkserver', 'spawn', 'dummy'
    ] | None = None,
    fail: bool = False,
) -> int:
    my_list: list[int] = list(range(1, length + 1))
    sublists: list[list[int]] = []
    subsums: list[int]
    sublength = length // n
    if sublength * n < length:
        sublength += 1
    while my_list:
        sublist, my_list = my_list[:sublength], my_list[sublength:]
        sublists.append(sublist)
    if start_method == 'dummy':
        pool = dummy.Pool(n)
    elif start_method:
        pool = get_context(start_method).Pool(n)
    else:
        pool = Pool(n)
    with pool:
        subsums = pool.starmap(my_sum, [(sl, fail) for sl in sublists])
        pool.close()
        pool.join()
    return my_sum(subsums, fail)


def main(args: list[str] | None = None) -> None:
    parser = ArgumentParser()
    parser.add_argument('-l', '--length', type=int, default={NUM_NUMBERS})
    parser.add_argument('-n', type=int, default={NUM_PROCS})
    parser.add_argument(
        '-s', '--start-method',
        choices=['fork', 'forkserver', 'spawn'], default=None,
    )
    parser.add_argument('-f', '--force-failure', action='store_true')
    parser.add_argument(
        '--local',
        action='store_const',
        dest='my_sum',
        default=my_external_sum,
        const=my_local_sum,
    )
    options = parser.parse_args(args)
    print(sum_in_child_procs(
        options.length, options.n, options.my_sum,
        start_method=options.start_method,
        fail=options.force_failure,
    ))


if __name__ == '__main__':
    main()
""")


# ============================== Fixtures ==============================


@dataclasses.dataclass
class _ModuleFixture:
    """
    Convenience wrapper around a Python source file which represents an
    importable module.
    """
    path: Path
    monkeypatch: pytest.MonkeyPatch
    dependencies: Collection[_ModuleFixture] = ()

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
            module: _ModuleFixture,
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


# Only write the files once per test session


@pytest.fixture(scope='session')
def _ext_module() -> Generator[Path, None, None]:
    name = next(_ModuleFixture.propose_name('my_ext_module'))
    with TemporaryDirectory() as mydir_str:
        my_dir = Path(mydir_str)
        my_dir.mkdir(exist_ok=True)
        my_module = my_dir / f'{name}.py'
        my_module.write_text(EXTERNAL_MODULE_BODY)
        yield my_module


@pytest.fixture(scope='session')
def _test_module(_ext_module: Path) -> Generator[Path, None, None]:
    name = next(_ModuleFixture.propose_name('my_test_module'))
    body = TEST_MODULE_TEMPLATE.format(
        EXT_MODULE=_ext_module.stem,
        NUM_NUMBERS=NUM_NUMBERS,
        NUM_PROCS=NUM_PROCS,
    )
    with TemporaryDirectory() as mydir_str:
        my_dir = Path(mydir_str)
        my_dir.mkdir(exist_ok=True)
        my_module = my_dir / f'{name}.py'
        my_module.write_text(body)
        yield my_module


@pytest.fixture
def ext_module(
    _ext_module: Path, monkeypatch: pytest.MonkeyPatch,
) -> Generator[_ModuleFixture, None, None]:
    """
    Yields:
        :py:class:`_ModuleFixture` helper object containing the code at
        :py:data:`EXTERNAL_MODULE_BODY`
    """
    yield _ModuleFixture(_ext_module, monkeypatch)


@pytest.fixture
def test_module(
    _test_module: Path,
    ext_module: _ModuleFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[_ModuleFixture, None, None]:
    """
    Yields:
        :py:class:`_ModuleFixture` helper object containing the code at
        :py:data:`TEST_MODULE_TEMPLATE`
    """
    yield _ModuleFixture(_test_module, monkeypatch, [ext_module])


@pytest.fixture
def test_module_clone(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    _test_module: Path,
    ext_module: _ModuleFixture,
) -> Generator[_ModuleFixture, None, None]:
    """
    Yields:
        :py:class:`_ModuleFixture` helper object containing the same
        code as :py:data:`test_module`
    """
    tmpdir = tmp_path_factory.mktemp('my_path')
    name = next(_ModuleFixture.propose_name('my_cloned_module'))
    path = tmpdir / f'{name}.py'
    path.write_text(_test_module.read_text())
    yield _ModuleFixture(path, monkeypatch, [ext_module])


@pytest.fixture
def ext_module_object(
    ext_module: _ModuleFixture,
) -> Generator[ModuleType, None, None]:
    """
    Yields:
        :py:class:`ModuleType` object containing the code at
        :py:data:`EXTERNAL_MODULE_BODY`, and is torn down at the end of
        the test
    """
    yield from ext_module._import_module_helper()


@pytest.fixture
def test_module_object(
    test_module: _ModuleFixture, ext_module_object: ModuleType,
) -> Generator[ModuleType, None, None]:
    """
    Yields:
        :py:class:`ModuleType` object containing the code at
        :py:data:`TEST_MODULE_TEMPLATE`, and is torn down at the end of
        the test
    """
    yield from test_module._import_module_helper()


@pytest.fixture
def create_cache(
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Generator[Callable[..., LineProfilingCache], None, None]:
    """
    Wrapper around the :py:class:`LineProfilingCache` instantiator
    which:

    - Automatically creates a tempdir and provides it as the
      :py:attr:`LineProfilingCache.cache_dir`,

    - Extends the argument ``preimports_module`` to allow for taking
      boolean values:

      - ``True``: a temporary preimports module is automatically written
        based on ``profiling_targets`` and supplied to the base
        constructor.

      - ``False``: equivalent to ``None``.

    - Unless the argument ``_use_curated_profiler: bool = True`` is set
      to :py:const:`False`, automatically creates an instance of
      :py:class:`LineProfiler` that is curated by a
      :py:class:`CuratedProfilerContext` and provides it as the
      :py:attr:`LineProfilingCache.profiler`, and

    - At teardown:

      - Removes tempdirs and tempfiles generated.

      - Restores the value of the class' internal reference to the
        :py:meth:`LineProfilingCache.load`-ed instance.

      - Calls the `.cleanup()` method of each instance created.

      - Prints these diagnostics for each instance:

        - The stats on the ``.profiler`` associated with each instance
          (if any)

        - The stats gathered by
          :py:meth:`LineProfilingCache.gather_stats()`

        - The debug logs (if ``.debug`` is true)
    """
    def instantiate(
        *,
        profiling_targets: Collection[str] = (),
        preimports_module: os.PathLike[str] | str | bool | None = None,
        _use_curated_profiler: bool = True,
        **kwargs
    ) -> LineProfilingCache:
        tmpdir = tmp_path_factory.mktemp('my_cache_dir')
        pim: os.PathLike[str] | str | None
        if preimports_module in (True, False):
            if preimports_module:
                targets = (
                    ClassifiedPreimportTargets.from_targets(profiling_targets)
                )
                if targets:
                    pim = tmpdir / 'preimports.py'
                    with pim.open(mode='w') as fobj:
                        targets.write_preimport_module(fobj)
                else:
                    pim = None
            else:
                pim = None
        else:
            # The type checker needs some convincing...
            assert not isinstance(preimports_module, bool)
            pim = preimports_module
        cache = LineProfilingCache(
            tmpdir,
            profiling_targets=profiling_targets,
            preimports_module=pim,
            **kwargs,
        )
        if _use_curated_profiler:
            cache.profiler = request.getfixturevalue('curated_profiler')
        instances.append(cache)
        return cache

    def print_result(
        cache: LineProfilingCache, topic: str, result: str, *notes: str,
    ) -> None:
        header = '{} ({}):'.format(
            topic, '; '.join([f'cache instance {id(cache):#x}', *notes]),
        )
        print(header, indent(result, '  '), sep='\n')

    def print_profiler_stats(cache: LineProfilingCache) -> None:
        if cache.profiler is None:
            result = '<N/A: no `.profiler` assigned>'
            notes = []
        else:
            with StringIO() as sio:
                cache.profiler.print_stats(sio)
                result = sio.getvalue()
            notes = [f'profiler instance {id(cache.profiler):#x}']
        print_result(cache, 'Native profiler stats', result, *notes)

    def print_gathered_stats(cache: LineProfilingCache) -> None:
        with StringIO() as sio:
            cache.gather_stats().print(sio)
            result = sio.getvalue()
        print_result(cache, 'Gathered profiler stats', result)

    def print_debug_logs(cache: LineProfilingCache) -> None:
        if cache.debug:
            result = '\n'.join(
                entry.to_text() for entry in cache._gather_debug_log_entries()
            )
        else:
            result = '<N/A: no debug logs>'
        print_result(cache, 'Gathered debug logs', result)

    instances: list[LineProfilingCache] = []
    handlers: list[Callable[[LineProfilingCache], None]]
    handlers = [print_profiler_stats, print_gathered_stats, print_debug_logs]
    try:
        with _preserve_obj_attributes(
            LineProfilingCache, ['_loaded_instance'],
        ):
            yield instantiate
    finally:
        for cache in instances:
            callbacks: list[Callable[[], Any]] = [cache.cleanup]
            callbacks.extend(partial(func, cache) for func in handlers)
            for callback in callbacks:
                try:
                    callback()
                except Exception:
                    pass


@pytest.fixture
def curated_profiler() -> Generator[LineProfiler, None, None]:
    """
    Yields:
        Fresh instance of :py:class:`LineProfiler` that is managed by a
        :py:class:`CuratedProfilerContext`
    """
    prof = LineProfiler()
    with CuratedProfilerContext(prof, insert_builtin=True):
        yield prof


@pytest.fixture
def another_pid() -> int:
    """
    Get a PID which is distinct from the current one.
    """
    curr_pid = os.getpid()
    pid = (curr_pid - 42) % (2 * 16)
    assert pid != curr_pid
    return pid


@pytest.fixture(autouse=True)
def _trim_mismatch_traceback(pytestconfig: pytest.Config) -> None:
    """
    Truncate the traceback of raised :py:class`ResultMismatch` for more
    useful error attribution.
    """
    try:
        pytestconfig.pluginmanager.register(ResultMismatch)
    except ValueError:  # Already registered
        pass


# ========================== Helper functions ==========================


class _NotSupplied(enum.Enum):
    NOT_SUPPLIED = enum.auto()


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
        actual: Any | _NotSupplied = _NotSupplied.NOT_SUPPLIED,
        _trunc_tb: int = 0,
    ) -> None:
        if actual == _NotSupplied.NOT_SUPPLIED:
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


class _TestTimeout(RuntimeError):
    """
    Error raised by the :py:func:`_timeout` decorator.
    """
    pass


@final
@dataclasses.dataclass
class _Params:
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
            >>> p1 = _Params.new(('a', 'b'), [(0, 0), (1, 2), (3, 4)],
            ...                  defaults=(1, 2))
            >>> p2 = _Params.new('c', [0, 5, 6])
            >>> p1 * p2  # doctest: +NORMALIZE_WHITESPACE
            _Params(params=('a', 'b', 'c'),
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
            >>> p1 = _Params.new(('a', 'b', 'c'),
            ...                  [(0, 0, 0),  # defaults
            ...                   (1, 2, 3), (4, 5, 6)])
            >>> p2 = _Params.new(('c', 'd'), [(7, 8), (9, 10)],
            ...                  defaults=(-1, -1))
            >>> p1 + p2  # doctest: +NORMALIZE_WHITESPACE
            _Params(params=('a', 'b', 'c', 'd'),
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
            >>> p = _Params.new(('a', 'b'), [(1, 2), (3, 4)])
            >>> p.drop_params('a')
            _Params(params=('b',), values=[(2,), (4,)], defaults=(2,))
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
            >>> p = _Params.new(('a', 'b', 'c'),
            ...                 [(1, 2, True),
            ...                  (1, 2, False),
            ...                  (3, 4, True)])

            >>> p.split_on_params('a')  # doctest: +NORMALIZE_WHITESPACE
            {1: _Params(params=('b', 'c'),
                        values=[(2, True), (2, False)],
                        defaults=(2, True)),
             3: _Params(params=('b', 'c'),
                        values=[(4, True)],
                        defaults=(2, True))}

            >>> p.split_on_params(  # doctest: +NORMALIZE_WHITESPACE
            ...     ('a', 'b'),
            ... )
            {(1, 2): _Params(params=('c',),
                             values=[(True,), (False,)],
                             defaults=(True,)),
             (3, 4): _Params(params=('c',),
                             values=[(True,)],
                             defaults=(True,))}

            >>> p.split_on_params(  # doctest: +NORMALIZE_WHITESPACE
            ...     'a', drop_split_params=False,
            ... )
            {1: _Params(params=('a', 'b', 'c'),
                        values=[(1, 2, True), (1, 2, False)],
                        defaults=(1, 2, True)),
             3: _Params(params=('a', 'b', 'c'),
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
        defaults: Sequence[Any] | _NotSupplied = _NotSupplied.NOT_SUPPLIED,
    ) -> Self:
        ...

    @overload
    @classmethod
    def new(
        cls,
        params: str,
        values: Sequence[Any],
        defaults: Any | _NotSupplied = _NotSupplied.NOT_SUPPLIED,
    ) -> Self:
        ...

    @classmethod
    def new(
        cls,
        params: Sequence[str] | str,
        values: Sequence[Sequence[Any]] | Sequence[Any],
        defaults: (
            Sequence[Any] | Any | _NotSupplied
        ) = _NotSupplied.NOT_SUPPLIED,
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
        if defaults == _NotSupplied.NOT_SUPPLIED:
            defaults, *_ = values
        if unpacked:
            default_values: tuple[Any, ...] = defaults,
            value_tuple_list: list[tuple[Any, ...]] = [(v,) for v in values]
        else:
            default_values = tuple(defaults)  # type: ignore[arg-type]
            value_tuple_list = [tuple(v) for v in values]
        return cls(param_list, value_tuple_list, default_values)


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


class _preserve_obj_attributes(_CallableContextManager[dict[str, Any]]):
    def __init__(
        self, obj: Any, attrs: Collection[str], *,
        static: bool = True, debug: bool = _DEBUG,
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
            old = get_attribute(self.obj, attr, _NotSupplied.NOT_SUPPLIED)
            if old is _NotSupplied.NOT_SUPPLIED:
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


class _preserve_attributes(_CallableContextManager[dict[str, dict[str, Any]]]):
    """
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
        >>> with _preserve_attributes(preserved, debug=False) as old:
        ...     assert old == {
        ...         'line_profiler.curated_profiling'
        ...         '.CuratedProfilerContext': {
        ...             'foo': _NotSupplied.NOT_SUPPLIED,
        ...         },
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
        static: bool = True, debug: bool = _DEBUG,
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
            result[target] = stack.enter_context(_preserve_obj_attributes(
                _import_target(target), attrs,
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
        na = _NotSupplied.NOT_SUPPLIED
        if static:
            get: _GetAttr = inspect.getattr_static
        else:
            get = getattr
        for target, attrs in targets.items():
            obj = _import_target(target)
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
            self.targets = _GLOBAL_PATCHES
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


class _preserve_pth_files(_CallableContextManager[frozenset[str]]):
    def __init__(self, debug: bool = _DEBUG) -> None:
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
    checks: list[tuple[_WarningMatcher, bool]] = dataclasses.field(
        default_factory=list,
    )

    def forbid_warnings(
        self,
        message: str | None = None,
        category: type[Warning] | None = Warning,
        module: str | None = None,
        lineno: int | None = None,
    ) -> None:
        matcher = _WarningMatcher(
            message=message, category=category, module=module, lineno=lineno,
        )
        self.checks.append((matcher, False))

    def expect_warnings(
        self,
        message: str | None = None,
        category: type[Warning] | None = Warning,
        module: str | None = None,
        lineno: int | None = None,
    ) -> None:
        matcher = _WarningMatcher(
            message=message, category=category, module=module, lineno=lineno,
        )
        self.checks.append((matcher, True))

    def check(self, warnings: Sequence[_WarningInfo]) -> None:
        for matcher, allowed_or_required in self.checks:
            matches = [info for info in warnings if matcher.match(info)]
            if matches and not allowed_or_required:
                raise ResultMismatch(
                    expected=f'no warnings matching {matcher!r}',
                    actual=f'{len(matches)} ({matches!r})',
                )
            if not matches and allowed_or_required:
                raise ResultMismatch(
                    expected=f'warnings matching {matcher!r}',
                    actual=f'none out of {len(warnings)} ({warnings!r})',
                )

    @classmethod
    def new(cls, **kwargs) -> Self:
        kwargs['record'] = True
        return cls(warnings.catch_warnings(**kwargs))


class _check_warnings(Sequence[_WarningInfo]):
    """
    Helper context for deferring the checking of warnings to until
    context exit.

    Example:
        >>> import warnings

        >>> cw = _check_warnings()

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
    def __init__(self, **kwargs) -> None:
        self._new_context: Callable[[], _WarningContext] = partial(
            _WarningContext.new, **kwargs,
        )
        self._contexts: list[
            tuple[_WarningContext, Sequence[_WarningInfo]]
        ] = []
        self._last_captured: Sequence[_WarningInfo] = []

    def forbid_warnings(self, *args, **kwargs) -> None:
        """
        Equivalent to calling
        ``filterwarnings('error', *args, **kwargs)``;
        at context exit, if ANY matching warning has been issued, an
        error will be raised.
        """
        ctx, _ = self._current_context
        ctx.forbid_warnings(*args, **kwargs)

    def expect_warnings(self, *args, **kwargs) -> None:
        """
        Equivalent to calling
        ``filterwarnings('always', *args, **kwargs)``;
        at context exit, if NO matching warnings have been issued, an
        error will be raised.
        """
        ctx, _ = self._current_context
        ctx.expect_warnings(*args, **kwargs)

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


def _import_target(target: str) -> Any:
    try:
        return import_module(target)
    except ImportError:  # Not a module
        assert '.' in target
        module, _, attr = target.rpartition('.')
        return getattr(import_module(module), attr)


def _search_cache_logs(
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


# `shlex.join()` doesn't work properly on Windows, so use
# `subprocess.list2cmdline()` instead;
# though an "intentionally" undocumented API (cpython issue #10308),
# it's been around since 2.4, seems stable enough, and does exactly what
# is needed
if _WINDOWS:
    concat_command_line: Callable[
        [Sequence[str]], str
    ] = subprocess.list2cmdline
else:
    concat_command_line = shlex.join


def _run_as_script(
    request: pytest.FixtureRequest,
    runner_args: list[str],
    test_args: list[str],
    test_module: _ModuleFixture,
    *,
    subproc: bool = True,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + [str(test_module.path)] + test_args
    if subproc:
        run: Callable[..., subprocess.CompletedProcess] = _run_subproc
    else:
        run = partial(_run_kernprof_main_in_process, request)
    test_module.install(children=True, local=not subproc, deps_only=True)
    return run(cmd, **kwargs)


def _run_as_module(
    request: pytest.FixtureRequest,
    runner_args: list[str],
    test_args: list[str],
    test_module: _ModuleFixture,
    *,
    subproc: bool = True,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + ['-m', test_module.name] + test_args
    if subproc:
        run: Callable[..., subprocess.CompletedProcess] = _run_subproc
    else:
        run = partial(_run_kernprof_main_in_process, request)
    test_module.install(children=True, local=not subproc)
    return run(cmd, **kwargs)


def _run_as_literal_code(
    request: pytest.FixtureRequest,
    runner_args: list[str],
    test_args: list[str],
    test_module: _ModuleFixture,
    *,
    subproc: bool = True,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + ['-c', test_module.path.read_text()] + test_args
    if subproc:
        run: Callable[..., subprocess.CompletedProcess] = _run_subproc
    else:
        run = partial(_run_kernprof_main_in_process, request)
    test_module.install(children=True, local=not subproc, deps_only=True)
    return run(cmd, **kwargs)


@_preserve_pth_files()
@_preserve_attributes()
def _run_kernprof_main_in_process(
    request: pytest.FixtureRequest, cmd: Sequence[str],
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
        main = _timeout(kernprof_main, timeout=timeout)
    else:
        main = kernprof_main
    try:
        try:
            with Cleanup() as cleanup:
                if env is not None:
                    cleanup.update_mapping(os.environ, env)
                main(cmd[1:], exit_on_error=False)
        except _TestTimeout:  # `subprocess` uses `SIGKILL`
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


def _run_subproc(
    cmd: Sequence[str] | str,
    /,
    *args,
    **kwargs
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


@_preserve_pth_files()
def _run_test_module(
    run_helper: Callable[..., subprocess.CompletedProcess],
    request: pytest.FixtureRequest,
    test_module: _ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    runner: str | list[str] = 'kernprof',
    outfile: str | None = None,
    profile: bool = True,
    *,
    profiled_code_is_tempfile: bool = False,
    use_local_func: bool = False,
    fail: bool = False,
    start_method: Literal['fork', 'forkserver', 'spawn'] | None = None,
    nnums: int | None = None,
    nprocs: int | None = None,
    check: bool = True,
    debug_log: str | None = None,
    nhits: Mapping[str, int] | None = None,
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
        runner_args: list[str] = [runner]
    else:
        runner_args = list(runner)

    if not profile:
        nhits = None

    if profile and not profiled_code_is_tempfile:
        runner_args.extend(['--prof-mod', str(test_module.path)])
    if nhits is not None:
        # We need `kernprof` to write the profliing results immediately
        # to preserve data from tempfiles (see note below)
        runner_args.append('--view')

    test_args: list[str] = []
    if use_local_func:
        test_args.append('--local')
    if fail:
        test_args.append('--force-failure')
    if start_method:
        if start_method in START_METHODS:
            test_args.extend(['-s', start_method])
        else:
            pytest.skip(
                f'`multiprocessing` start method {start_method!r} '
                'not available on the platform'
            )
    if nnums is None:
        nnums = NUM_NUMBERS
    else:
        test_args.extend(['-l', str(nnums)])
    if nprocs is not None:
        test_args.extend(['-n', str(nprocs)])

    with ub.ChDir(tmp_path_factory.mktemp('mytemp')):
        if outfile is not None:
            runner_args.extend(['--outfile', outfile])
        if debug_log:
            runner_args.extend(['--debug-log', debug_log])
        old_pth_files = _preserve_pth_files.get_pth_files()
        try:
            proc = run_helper(
                request, runner_args, test_args, test_module,
                text=True, capture_output=True, check=(check and not fail),
                **kwargs
            )
            # Checks:
            if fail:
                # - The process has failed as expected
                if check:
                    assert proc.returncode
            else:
                # - The result is correctly calculated
                expected = nnums * (nnums + 1) // 2
                output_lines = proc.stdout.splitlines()
                ResultMismatch.compare(str(expected), output_lines[0])
            # - Temporary `.pth` file(s) created by
            # `LineProfilingCache.write_pth_hook()` has been cleaned up
            assert _preserve_pth_files.get_pth_files() == old_pth_files
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
                _check_output(proc.stdout, tag, num)
        finally:
            if debug_log is not None and os.path.exists(debug_log):
                with open(debug_log) as fobj:
                    print('-- Combined debug logs --', file=sys.stderr)
                    print(indent(fobj.read(), '  '), end='', file=sys.stderr)
                    print('-- End of debug logs --', file=sys.stderr)
    return proc, prof_result


def _check_output(output: str, tag: str, nhits: int) -> None:
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


def _cleanup_profiling_in_current_thread() -> None:
    """
    Disable all active profiler instances on the current thread, so that
    we don't trip over ourselves if a new thread down the road happens
    to reuse the thread ID.

    Note:
        The thread-local profiler state is supposed to be handled by
        :py:mod:`line_profiler._threading_patches` and
        :py:class:`.CuratedProfilerContext`, but since a test function
        decorated with :py:deco:`._timeout` will be isolated in a new
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
def _timeout(
    func: Callable[PS, T], *, timeout: float = _TEST_TIMEOUT,
) -> Callable[PS, T]:
    ...


@overload
def _timeout(
    func: None = None, *, timeout: float = _TEST_TIMEOUT,
) -> Callable[[Callable[PS, T]], Callable[PS, T]]:
    ...


def _timeout(
    func: Callable[PS, T] | None = None, *,
    timeout: float = _TEST_TIMEOUT,
) -> Callable[PS, T] | Callable[[Callable[PS, T]], Callable[PS, T]]:
    """
    Decorate the test function so that it is run in another thread and
    can be timed out.

    Example:
        >>> from time import sleep

        >>> @_timeout(timeout=.5)
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
        test_child_procs._TestTimeout:
        my_func(4, delay=5): timed out after 0.5 s
    """
    if func is None:
        return cast(
            Callable[[Callable[PS, T]], Callable[PS, T]],
            partial(_timeout, timeout=timeout),
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
        raise _TestTimeout(msg)

    new_thread = partial(threading.Thread, target=worker, daemon=True)

    return wrapper


# ============================= Unit tests =============================

# XXX: Tests in this section concern implementation details, and the
# tested APIs and behaviors MUST NOT be relied upon by end-users.

_PatchSummary = Mapping[str, Set[str]]

_mp_patch_is_internal: Callable[[str], bool]
_mp_patch_is_internal = operator.methodcaller('startswith', '__')


def get_patched_attributes(
    applied_mp_patches: Collection[str] | None = None,
) -> MappingProxyType[str, frozenset[str]]:
    if applied_mp_patches is None:
        applied_mp_patches = {
            patch for patch, applied in (
                ConfigSource.from_config()
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
    patches = _GLOBAL_MINIMAL_PATCHES.copy()
    iter_summaries = (
        MP_PATCHES[patch].summary
        for patch in applied_mp_patches if patch in MP_PATCHES
    )
    patches = _get_patch_summary_union(patches, *iter_summaries)
    return MappingProxyType({
        target: frozenset(attrs)
        for target, attrs in _filter_patches(patches).items()
    })


def _get_toml_patches_section(mp_patches: Collection[str]) -> str:
    mp_patches_as_dict = {
        name: name in mp_patches for name in MP_PATCHES
        if not _mp_patch_is_internal(name)
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


def _summarize_patches(
    summaries: Collection[tuple[bool, _PatchSummary]]
) -> dict[str, dict[str, bool]]:
    """
    Example:
        >>> _summarize_patches([(False, {'foo': {'bar'}})])
        {'foo': {'bar': False}}
        >>> _summarize_patches([  # doctest: +NORMALIZE_WHITESPACE
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


def _filter_patches(summary: _PatchSummary) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for target, attrs in summary.items():
        try:
            obj = _import_target(target)
        except ImportError:
            continue
        present_attrs = {a for a in attrs if hasattr(obj, a)}
        # Drop if none of the attributes is present
        if present_attrs:
            result[target] = present_attrs
    return result


_GLOBAL_MINIMAL_PATCHES = {
    'multiprocessing': frozenset({_PATCHED_MARKER}),
}
# Get patches that are dynamically resolved: while these patches are
# always applied, some of the patch targets are
# platform-/Pyhon-version-specific and may not always exist
_dynamically_resolved_patch_summaries: Iterable[_PatchSummary] = (
    patch.summary for name, patch in MP_PATCHES.items()
    # Basic `multiprocessing` patches are always applied
    if _mp_patch_is_internal(name)
)
_dynamically_resolved_patch_summaries = itertools.chain(
    _dynamically_resolved_patch_summaries,
    # some platforms e.g. Windows don't have `fork()`
    [{'os': frozenset({'fork'})}],
)
_dynamically_resolved_patch_summaries = cast(  # See `ty` issue #3428
    Iterable[_PatchSummary],
    map(_filter_patches, _dynamically_resolved_patch_summaries),
)
_GLOBAL_MINIMAL_PATCHES = _get_patch_summary_union(
    _GLOBAL_MINIMAL_PATCHES, *_dynamically_resolved_patch_summaries,
)

# This is only patched if we called
# `_line_profiler_hooks.load_pth_hook()`
_HOOK_PATCHES = {
    f'{load_pth_hook.__module__}.{load_pth_hook.__qualname__}':
        frozenset({'called'}),
}
# Upper limit of what we could've patched
_GLOBAL_PATCHES = _get_patch_summary_union(
    _GLOBAL_MINIMAL_PATCHES,
    _HOOK_PATCHES,
    get_patched_attributes([
        name for name in MP_PATCHES if not _mp_patch_is_internal(name)
    ]),
)
# Actual patches using the default config
DEFAULT_GLOBAL_PATCHES = _get_patch_summary_union(
    _GLOBAL_MINIMAL_PATCHES, get_patched_attributes(),
)

_DEFAULT_MP_CONFIG = MPConfig.from_config(ConfigSource.from_default())


@pytest.mark.parametrize(('run_profiled_code', 'label1'),
                         [(True, 'run-profiled'), (False, 'run-unrelated')])
@pytest.mark.parametrize(('as_module', 'label2'),
                         [(True, 'run_module'), (False, 'run_path')])
@pytest.mark.parametrize(('debug', 'label3'),
                         [(True, 'with-debug'), (False, 'no-debug')])
def test_runpy_patches(
    capsys: pytest.CaptureFixture[str],
    ext_module: _ModuleFixture,
    test_module: _ModuleFixture,
    test_module_clone: _ModuleFixture,
    create_cache: Callable[..., LineProfilingCache],
    run_profiled_code: bool,
    as_module: bool,
    debug: bool,
    label1: str, label2: str, label3: str,
) -> None:
    """
    Test that the :py:mod:`runpy` clone created by
    :py:func:`line_profiler._child_process_profiling\
.create_runpy_wrapper`
    correctly sets up profiling when its ``run_*()`` functions are
    called.
    """
    class restore_argv:
        def __enter__(self) -> None:
            self.argv = list(sys.argv)

        def __exit__(self, *_, **__) -> None:
            sys.argv[:] = self.argv

    cache = create_cache(
        rewrite_module=test_module.path,
        profiling_targets=[str(ext_module.path)],
        profile_imports=True,
        debug=debug,
    )
    assert cache.profiler is not None
    runpy = create_runpy_wrapper(cache)

    nnums = 42
    nprocs = 2
    # If we're running some unrelated code, the profiler should not be
    # involved
    if run_profiled_code:
        module = test_module
        num_invocations, num_loops = 1, nprocs
        expected_funcs: list[str] = ['my_external_sum']
    else:
        module = test_module_clone
        num_invocations, num_loops = 0, 0
        expected_funcs = []
    if as_module:
        first_arg = module.name
        runner = partial(runpy.run_module, alter_sys=True)
        called_func = 'run_module'
    else:
        first_arg = str(module.path)
        runner = runpy.run_path
        called_func = 'run_path'

    # Check that the code is run
    module.install(local=True, deps_only=not as_module)
    with restore_argv():
        sys.argv[:] = [first_arg, f'--length={nnums}', '-n', str(nprocs)]
        runner(first_arg, run_name='__main__')
    stdout = capsys.readouterr().out
    assert stdout.rstrip('\n') == str(nnums * (nnums + 1) // 2)

    # Check that profiler has received the appropriate targets
    funcs = [func.__name__ for func in getattr(cache.profiler, 'functions')]
    assert funcs == expected_funcs

    # Check that calls in the current process are profiled iif the
    # correct file is executed
    with StringIO() as sio:
        cache.profiler.print_stats(sio)
        stats = sio.getvalue()
    _check_output(stats, 'EXT-INVOCATION', num_invocations)
    _check_output(stats, 'EXT-LOOP', num_loops)

    # Check the debug-log entries are correctly gathered
    _search_cache_logs(
        cache,
        debug,
        {
            rf'calling .*{called_func}\(': True,
            r'calling .*exec\(': run_profiled_code,
        },
        match_individual_messages=True,
        flags=re.IGNORECASE,
    )


def test_cache_dump_load(
    create_cache: Callable[..., LineProfilingCache],
) -> None:
    """
    Test that:

    - We can round-trip the cache via :py:meth:`LineProfilingCache.dump`
      and :py:meth:`LineProfilingCache.load`

    - The same instance is :py:meth:`LineProfilingCache.load`-ed in
      subsequent calls
    """
    original = create_cache(
        profiling_targets=['foo', 'bar', 'baz'], main_pid=123456,
    )
    cache_instances: list[LineProfilingCache] = [original]
    envvars: set[str] = set(os.environ)
    try:
        original.inject_env_vars()  # Needed for `.load()`
        # Also test slipping stuff into the `._additional_data`
        original._additional_data['foo'] = [1, 'string', None]
        try:
            # Env vars should be inserted
            assert set(os.environ) == envvars.union(original.environ) > envvars
            original.dump()
            loaded = original.load()
            cache_instances.append(loaded)
            reloaded = original.load()
            cache_instances.append(reloaded)
            assert original is not loaded is reloaded
            # Compare init fields
            for field in dataclasses.fields(LineProfilingCache):
                if not field.init:
                    continue
                assert (
                    getattr(original, field.name)
                    == getattr(loaded, field.name)
                )
            # Compare `._additional_data`
            assert original._additional_data == loaded._additional_data
        finally:  # Explicitly cleanup
            for cache in cache_instances:
                cache.cleanup()
    finally:  # Env vars restored after cleanup
        assert set(os.environ) == envvars


@(_Params.new(('wrap_os_fork', 'label1'),
              [(True, 'with-wrap-fork'), (False, 'no-wrap-fork')])
  + _Params.new(('debug', 'label2'),
                [(True, 'with-debug'), (False, 'no-debug')])
  + _Params.new(('patch_pool', 'patch_process', 'intercept_logs', 'label3'),
                [(True, True, True, 'all-patches'),
                 (True, True, False, 'pool-and-process'),
                 (True, False, True, 'pool-and-logging'),
                 (True, False, False, 'pool-only'),
                 (False, True, True, 'process-and-logging'),
                 (False, True, False, 'process-only'),
                 (False, False, True, 'logging-only'),
                 (False, False, False, 'no-patches')])).sorted()
def test_cache_setup_main_process(
    tmp_path_factory: pytest.TempPathFactory,
    create_cache: Callable[..., LineProfilingCache],
    wrap_os_fork: bool,
    debug: bool,
    patch_pool: bool,
    patch_process: bool,
    intercept_logs: bool,
    label1: str, label2: str, label3: str,
) -> None:
    """
    Test that :py:meth:`LineProfilingCache._setup_in_main_process` works
    as expected.
    """
    mp_patches: set[str] = set()
    if patch_pool:
        mp_patches.add('pool')
    if patch_process:
        mp_patches.add('process')
    if intercept_logs:
        mp_patches.add('logging')

    config = tmp_path_factory.mktemp('myconfig') / 'mytoml.toml'
    config.write_text(_get_toml_patches_section(mp_patches))
    cache = create_cache(debug=debug, config=config)

    # Check that only the requested patches are applied
    patches = _summarize_patches([
        (True, _GLOBAL_MINIMAL_PATCHES),
        *(
            (name in mp_patches, _filter_patches(patch.summary))
            for name, patch in MP_PATCHES.items()
            if not _mp_patch_is_internal(name)
        ),
    ])
    try:
        patches['os']['fork'] = wrap_os_fork
    except KeyError:
        # `os.fork()` pruned because it doesn't exist on e.g. Windows
        assert not hasattr(os, 'fork')

    with ExitStack() as stack:
        patched = stack.enter_context(_preserve_attributes(patches))
        compare_patched = partial(
            _preserve_attributes.compare_with_current_values, patched,
        )
        original_pths = stack.enter_context(_preserve_pth_files())
        cache._setup_in_main_process(wrap_os_fork=wrap_os_fork)
        # There should be exactly one extra `.pth` file
        new_pth_hook, = _preserve_pth_files.get_pth_files() - original_pths
        # Check whether the patches are applied
        compare_patched(operator.is_not, assert_true=patches)
        # Check that the instance is set as the `.load()`-ed one
        assert cache is cache.load()
        # Check whether the patches are reversed
        cache.cleanup()
        compare_patched()

    # Check the debug-log output
    patterns: dict[str, bool] = dict.fromkeys(
        [
            r'\(main process\)',
            r'Injecting env var.*\$\{LINE_PROFILER_\w+\}',
            re.escape(new_pth_hook),
        ],
        True,
    )
    for target, maybe_patches in patches.items():
        patterns.update(
            ('Patched.*' + re.escape(f'{target}.{attr}'), is_patched)
            for attr, is_patched in maybe_patches.items()
        )
    _search_cache_logs(cache, debug, patterns)


@pytest.mark.parametrize(('wrap_os_fork', 'label1'),
                         [(True, 'with-wrap-fork'), (False, 'no-wrap-fork')])
@pytest.mark.parametrize(('preimports', 'label2'),
                         [(True, 'with-preimports'), (False, 'no-preimports')])
@pytest.mark.parametrize(('new_profiler', 'label3'),
                         [(True, 'no-profiler'), (False, 'with-profiler')])
@pytest.mark.parametrize(('debug', 'label4'),
                         [(True, 'with-debug'), (False, 'no-debug')])
@pytest.mark.parametrize('n', [100])
@_preserve_attributes()
def test_cache_setup_child(
    create_cache: Callable[..., LineProfilingCache],
    ext_module_object: ModuleType,
    another_pid: int,
    wrap_os_fork: bool,
    preimports: bool,
    new_profiler: bool,
    debug: bool,
    n: int,
    label1: str, label2: str, label3: str, label4: str,
) -> None:
    """
    Test that :py:meth:`LineProfilingCache._setup_in_child_process`
    works as expected.
    """
    def list_profiled_funcs() -> list[str]:
        return [
            f'{func.__module__}.{func.__qualname__}'
            for func in getattr(cache.profiler, 'functions', [])
        ]

    func = ext_module_object.my_external_sum
    cache = create_cache(
        profiling_targets=[f'{func.__module__}.{func.__qualname__}'],
        preimports_module=preimports,
        _use_curated_profiler=not new_profiler,
        main_pid=another_pid,
        debug=debug,
    )
    assert (cache.profiler is None) == new_profiler

    seen_funcs = list_profiled_funcs()
    if preimports:
        preimport_targets = list(cache.profiling_targets)
    else:
        preimport_targets = []

    with _preserve_obj_attributes(os, ['fork']) as preserved:
        old_fork = preserved['fork']
        # Check that we're only setting up if there isn't already a
        # profiler
        assert cache._setup_in_child_process(
            wrap_os_fork=wrap_os_fork, context='test_cache_setup_child',
        ) == new_profiler
        assert cache.profiler
        if not new_profiler:
            return

        # Check that the profiler has been presented with the profiling
        # target
        assert list_profiled_funcs() == (seen_funcs + preimport_targets)

        # Check that on cache cleanup:
        # - Profiling data is collected
        # - `os.fork()` is restored
        # - The warning for empty profiling files is only issued when
        #   expected
        assert func(range(1, n + 1)) == n * (n + 1) // 2
        stats = cache.profiler.get_stats()
        for callback, has_nonempty_file, has_stats, fork_patched in [
            (lambda: None, False, False, wrap_os_fork),
            (cache.cleanup, True, preimports, False),
        ]:
            callback()
            with _check_warnings() as cw:
                if has_nonempty_file:
                    check_warning = cw.forbid_warnings
                else:
                    check_warning = cw.expect_warnings
                check_warning(r'.* file\(s\) .* empty', module='line_profiler')
                gathered = cache.gather_stats()
            assert any(gathered.timings.values()) == has_stats, gathered
            if hasattr(os, 'fork'):
                assert (os.fork is not old_fork) == fork_patched
            else:  # E.g. Windows
                assert old_fork == _NotSupplied.NOT_SUPPLIED
    # Check that after cleaning up the profiler has been disabled
    assert not getattr(cache.profiler, 'enable_count', 0)

    # Check that profiling results have been written to the cache
    # directory
    stats_file, = Path(cache.cache_dir).glob('*.lprof')
    assert LineStats.from_files(stats_file) == stats == gathered

    # Check the debug-log output
    patterns = {
        f'Set up .*profiler.* {id(cache.profiler):#x}': True,
        'Loading preimports': preimports,
        'Created .*' + re.escape(stats_file.name): True,
        'Cleanup succeeded.*: .*dump_stats': True,
        'Loading results .*' + re.escape(stats_file.name): True,
    }
    _search_cache_logs(cache, debug, patterns)


@pytest.mark.parametrize('ppid_should_match', [True, False, None])
@_preserve_attributes()
def test_load_pth_hook(
    create_cache: Callable[..., LineProfilingCache],
    another_pid: int,
    ppid_should_match: bool | None,
) -> None:
    """
    Simulate calling :py:func:`_line_profiler_hooks.load_pth_hook()` in
    a child process.

    Notes:

        - The function is CALLED in the .pth file, but we don't actually
          NEED a .pth file to call and test it.

        - The counterpart :py:meth:`line_profiler\
._child_process_profiling.cache.LineProfilingCache.write_pth_hook()`
          is implicitly tested in
          :py:func:`test_cache_setup_main_process()`.
    """
    # This test is mostly here to hack coverage; since the function is
    # only to be called in child processes, `coverage` seems to have
    # trouble getting data on it...

    # We basically only need this cache instance to set up the
    # environment variables and the requisite files...
    cache = create_cache(main_pid=another_pid)
    if ppid_should_match is not None:
        cache.inject_env_vars()
        if ppid_should_match:
            call_ppid = another_pid
        else:  # On a PPID mismatch, the function bails after checking
            call_ppid = another_pid + 10
    else:
        # Without the requisite envvars, the hook should bail very
        # quickly (due to the `environ` lookup erroring out), regardless
        # of the provided PPID
        call_ppid = 0
    cache.dump()

    compare = _preserve_attributes.compare_with_current_values
    patches = {**DEFAULT_GLOBAL_PATCHES, **_HOOK_PATCHES}
    with _preserve_attributes(patches) as patched:
        try:
            # NOTE: this creates a cache instance that isn't
            # automatically cleaned up by the `create_cache()`
            # fixture!!! Hence the try-finally
            load_pth_hook(call_ppid)
            # Check that the patches are applied where appropriate
            assert (
                getattr(load_pth_hook, 'called', False)
                == bool(ppid_should_match)
            )
            if ppid_should_match:
                compare(patched, operator.is_not)
            else:  # no-op
                compare(patched)
                return
            # Check that calling `load_pth_hook()` again is a no-op
            with _preserve_attributes(patches) as re_patched:
                load_pth_hook(call_ppid)
                compare(re_patched)
        finally:
            try:
                current_cache = LineProfilingCache.load()
            except Exception:
                pass
            else:
                current_cache.cleanup()
        # Check that the patches are reversed
        compare(patched)


@_preserve_pth_files()
@_preserve_attributes()
@_timeout
def _test_apply_mp_patches_inner(
    tmp_path_factory: pytest.TempPathFactory,
    create_cache: Callable[..., LineProfilingCache],
    ext_module_object: ModuleType,
    test_module_object: ModuleType,
    start_method: Literal['fork', 'forkserver', 'spawn', 'dummy'],
    mp_patches: Collection[str],
    fail: bool,
    n: int,
    nprocs: int,
) -> None:
    def is_valid_stats_file(path: os.PathLike[str] | str) -> bool:
        try:
            LineStats.from_files(path, on_empty='error', on_defective='error')
        except Exception:
            return False
        return True

    def get_lineno(path: os.PathLike[str] | str, query: str) -> int:
        with Path(path).open() as fobj:
            for i, line in enumerate(fobj):
                if query in line:
                    return 1 + i
        raise RuntimeError(
            f'Did not find line containing {query!r} in {path!r}',
        )

    config = tmp_path_factory.mktemp('myconfig') / 'mytoml.toml'
    intercept_logs = 'logging' in mp_patches
    patch_process = 'process' in mp_patches
    cfg_chunks: list[str] = [
        _get_toml_patches_section(mp_patches),
        # This is easier to debug than `ResultMismatch`
        '[tool.line_profiler.child_processes.multiprocessing.polling]\n'
        'on_timeout = "error"',
    ]
    config.write_text('\n\n'.join(cfg_chunks))

    # Note: no need to test the case for `my_local_sum()` separately,
    # with `preimports_module=True`, both are just imported and added
    # to the profiler, so the code paths are the same
    profiled_func = ext_module_object.my_external_sum
    called_func = partial(
        test_module_object.sum_in_child_procs,
        n=nprocs,
        my_sum=profiled_func,
        start_method=start_method,
        fail=fail,
    )

    func_name = f'{profiled_func.__module__}.{profiled_func.__qualname__}'
    cache = create_cache(
        profiling_targets=[func_name],
        preimports_module=True,
        config=config,
        debug=True,
    )
    # Note:
    # - The reversibility of the patches have already been tested in
    #   `test_cache_setup_main_process()`, so we just actually test the
    #   patched-in components themselves here.
    # - `._setup_in_main_process()` doesn't include actually doing the
    #   preimports. To may the results more consistent between
    #   `start_method='dummy'` and the others, manually do them below.
    cache._setup_in_main_process()  # This calls `apply()`
    assert cache.profiler is not None
    assert cache.preimports_module is not None
    run_path(str(cache.preimports_module), {'profile': cache.profiler})

    timing_key = (
        inspect.getfile(profiled_func),
        inspect.getsourcelines(profiled_func)[1],
        profiled_func.__qualname__,
    )
    assert ext_module_object.__file__
    loop_line = get_lineno(ext_module_object.__file__, 'EXT-LOOP')

    nloops_expected = n
    if not fail:
        # Counts from the one final sum over the parallel results
        nloops_expected += nprocs

    if start_method not in ('dummy', *START_METHODS):
        pytest.skip(
            f'`multiprocessing` start method {start_method!r} '
            'not available on the platform'
        )

    # Note: manually handle the error here instead of using
    # `pytest.raises()` since we want certain `RuntimeError`s to be
    # propagated and handled by `@pytest.mark.retry`
    fail_msg = 'forced failure'
    try:
        result = called_func(n)
    except RuntimeError as e:
        if not (fail and str(e) == fail_msg):
            raise
    else:
        if fail:
            msg = f"expected `RuntimeError({fail_msg!r})`, no error raised"
            raise ValueError(msg)
        else:  # Check correctness of the results
            assert result == n * (n + 1) // 2

    # Check that calls in children are traced
    cache.cleanup()
    stats = cache.profiler.get_stats()
    stats += cache.gather_stats()
    entries = stats.timings[timing_key]
    nloops = sum(nhits for ln, nhits, _ in entries if ln == loop_line)
    ResultMismatch.compare(nloops_expected, nloops)

    # Check the debug logs to see if we have done everything right, esp.
    # the logging interception part not covered by other tests
    patterns: dict[str, bool] = {}
    if patch_process:
        # Note: if we're not using `Process`-based patch, there is no
        # guaratee that the profiling result is written via cleanup
        iter_stats: Iterable[Path] = Path(cache.cache_dir).glob('*.lprof')
        iter_stats = cast(  # See `ty` issue #3428
            Iterable[Path], filter(is_valid_stats_file, iter_stats),
        )
        pat = 'Cleanup succeeded.*: .*dump_stats.*{}'
        patterns.update({
            pat.format(re.escape(path.name)): True for path in iter_stats
        })
    patterns[re.escape('`multiprocessing` logging (debug)')] = intercept_logs
    _search_cache_logs(cache, True, patterns)


def _test_apply_mp_patches(
    patch_pool: bool | None = None,
    patch_process: bool | None = None,
    intercept_logs: bool | None = None,
    trace_pids: bool | None = None,
    **kwargs
) -> None:
    patches = cast(dict[str, bool], _DEFAULT_MP_CONFIG.patches.copy())
    for name, applied in {
        'pool': patch_pool, 'process': patch_process,
        'logging': intercept_logs, 'child_pids': trace_pids,
    }.items():
        if applied is not None:
            patches[name] = applied
    mp_patches = [name for name, applied in patches.items() if applied]
    with _check_warnings() as cw:
        if 'child_pids' in mp_patches:
            # With PID bookkeeping we should be able to weed out all the
            # child processes which didn't perform any work
            cw.forbid_warnings(category=UserWarning, module='line_profiler')
        cw.forbid_warnings(module='multiprocessing')
        _test_apply_mp_patches_inner(mp_patches=mp_patches, **kwargs)


@(_Params.new('start_method',
              ['fork', 'forkserver', 'spawn', 'dummy'],
              defaults='dummy')
  # We only need to check if `intercept_logs = logging` work, the other
  # parametrizations don't matter
  + _Params.new(('intercept_logs', 'label1'),
                [(True, 'with-logging'), (False, 'no-logging')],
                defaults=(None, 'default-logging'))
  # Same deal with `trace_pids = child_pids`
  + _Params.new(('trace_pids', 'label2'),
                [(True, 'with-child_pids'), (False, 'no-child-pids')],
                defaults=(None, 'default-child-pids'))).sorted()
@pytest.mark.parametrize(('patch_pool', 'patch_process', 'label3'),
                         [(True, True, 'pool-and-process'),
                          (True, False, 'pool-only'),
                          (False, True, 'process-only')])
@pytest.mark.parametrize(('n', 'nprocs'), [(100, 2)])
def test_apply_mp_patches_success(
    tmp_path_factory: pytest.TempPathFactory,
    create_cache: Callable[..., LineProfilingCache],
    ext_module_object: ModuleType,
    test_module_object: ModuleType,
    start_method: Literal['fork', 'forkserver', 'spawn', 'dummy'],
    patch_pool: bool,
    patch_process: bool,
    intercept_logs: bool | None,
    trace_pids: bool | None,
    n: int,
    nprocs: int,
    label1: str,
    label2: str,
    label3: str,
) -> None:
    """
    Test that :py:func:`line_profiler._child_process_profiling\
.multiprocessing_patches.apply`
    works as expected when the parallel workload does not error out.

    See also:
        :py:func:`test_apply_mp_patches_failure`
    """
    _test_apply_mp_patches(
        patch_pool,
        patch_process,
        intercept_logs,
        trace_pids,
        tmp_path_factory=tmp_path_factory,
        create_cache=create_cache,
        ext_module_object=ext_module_object,
        test_module_object=test_module_object,
        start_method=start_method,
        fail=False,
        n=n,
        nprocs=nprocs,
    )


# XXX: on POSIX child processes can hang around for long enough for
# profiling-stats collection to occur somewhat robustly, thanks to
# signal handling. But unfortunately on Windows:
# - When `patch_pool` is true, we wrap the task callables so that they
#   always write profiling stats before returning/erroring out. This
#   incurs extra overhead, but effectively prevents the reliquishing of
#   control back to the parent process before the stats are ready.
# - However, when `patch_pool` is false, we can only try to block/delay
#   child-process termination. A timeout is used to prevent indefinite
#   waits for them to finish, and there's always the off chance that the
#   end-of-process cleanup still haven't finished at the end.
# Hence the conditional need for retries...
@pytest.mark.retry(
    retries=2,
    condition='_WINDOWS and not patch_pool',
    exceptions=(ResultMismatch, _Poller.Timeout),
)
@pytest.mark.parametrize('start_method',
                         ['fork', 'forkserver', 'spawn', 'dummy'])
@pytest.mark.parametrize(('patch_pool', 'patch_process', 'label'),
                         [(True, True, 'pool-and-process'),
                          (True, False, 'pool-only'),
                          (False, True, 'process-only')])
@pytest.mark.parametrize(('n', 'nprocs'), [(100, 2)])
def test_apply_mp_patches_failure(
    tmp_path_factory: pytest.TempPathFactory,
    create_cache: Callable[..., LineProfilingCache],
    ext_module_object: ModuleType,
    test_module_object: ModuleType,
    start_method: Literal['fork', 'forkserver', 'spawn', 'dummy'],
    patch_pool: bool,
    patch_process: bool,
    n: int,
    nprocs: int,
    label: str,
) -> None:
    """
    Test that :py:func:`line_profiler._child_process_profiling\
.multiprocessing_patches.apply`
    works as expected when the parallel workload errors out.

    See also:
        :py:func:`test_apply_mp_patches_success`
    """
    _test_apply_mp_patches(
        patch_pool,
        patch_process,
        tmp_path_factory=tmp_path_factory,
        create_cache=create_cache,
        ext_module_object=ext_module_object,
        test_module_object=test_module_object,
        start_method=start_method,
        fail=True,
        n=n,
        nprocs=nprocs,
    )


# XXX: End of tests for implementation details

# ========================= Integration tests ==========================


def _get_mp_start_method_fuzzer(label_name: str | None) -> _Params:
    """
    Returns:
        :py:class:`_Params` object which does a full Cartesian-product
        fuzz between ``fail`` (true or false) and ``start_method``
        ('fork', 'forkserver', and 'spawn'; default :py:const:`None`)
    """
    if label_name is None:
        label_name, drop_label = '_', True
    else:
        drop_label = False
    fuzz_fail = _Params.new(('fail', label_name),
                            [(True, 'failure'), (False, 'success')],
                            defaults=(False, 'success'))
    if drop_label:
        fuzz_fail = fuzz_fail.drop_params(label_name)
    fuzz_start = _Params.new('start_method', ['fork', 'forkserver', 'spawn'],
                             defaults=None)
    return fuzz_fail * fuzz_start


@(_Params.new(('run_func', 'label1'),
              [(run_module, 'module'), (run_script, 'script')])
  * _Params.new(('use_local_func', 'label2'),
                [(True, 'local'), (False, 'ext')])
  # Python can't pickle things unless they resided in a retrievable
  # location (so not the script supplied by `python -c`)
  + _Params.new(('run_func', 'label1', 'use_local_func', 'label2'),
                [(run_literal_code, 'literal-code', False, 'ext')])
  # Also fuzz the parallelization-related stuff, esp. check what
  # happens if an exception is raised inside the parallelly-run func
  + _get_mp_start_method_fuzzer('label3')
  + _Params.new(('nnums', 'nprocs'), [(200, None), (None, 3)],
                defaults=(None, None))).sorted()
def test_multiproc_script_sanity_check(
    run_func: Callable[..., subprocess.CompletedProcess],
    request: pytest.FixtureRequest,
    test_module: _ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    use_local_func: bool,
    fail: bool,
    start_method: Literal['fork', 'forkserver', 'spawn'] | None,
    nnums: int | None,
    nprocs: int | None,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str, label3: str,
) -> None:
    """
    Sanity check that the test module functions as expected when run
    with vanilla Python.
    """
    run_func(
        request, test_module, tmp_path_factory,
        runner=sys.executable, profile=False,
        fail=fail,
        use_local_func=use_local_func,
        start_method=start_method,
        nnums=nnums, nprocs=nprocs,
    )


@pytest.mark.parametrize(
    ('run_func', 'label1'),
    [(run_module, 'module'),
     (run_script, 'script'),
     (run_literal_code, 'literal-code')]
)
@pytest.mark.parametrize(
    ('runner', 'outfile', 'profile',
     'label2'),  # Dummy argument to make `pytest` output more legible
    # This is essentially a no-op since it doesn't actually do
    # line-profiling, but we check that code path for completeness
    [(['kernprof', '-q', '--no-line'], 'out.prof', False, 'cProfile')]
    # Run line profiling with and w/o profiling targets
    + [(['kernprof', '-q', '-l'], 'out.lprof', False,
        'line_profiler-inactive'),
       (['kernprof', '-q', '-l'], 'out.lprof', True,
        'line_profiler-active')],
)
def test_running_multiproc_script(
    run_func: Callable[..., subprocess.CompletedProcess],
    request: pytest.FixtureRequest,
    test_module: _ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    runner: str | list[str],
    outfile: str | None,
    profile: bool,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str,
) -> None:
    """
    Check that `kernprof` can RUN the test module in various contexts
    (`kernprof [...] <path>`, `kernprof [...] -m <module>`, and
    `kernprof [...] -c "code"`).

    Notes:
        - See issue #422 for the original motivation.

        - This test does not test the actual profiling, just the
          execution of the code and presence of profiling data
          thereafter.
    """
    run_func(request, test_module, tmp_path_factory, runner, outfile, profile)


_fuzz_prof_mp_run_func = _Params.new(('run_func', 'label1'),
                                     [(run_module, 'module'),
                                      (run_script, 'script'),
                                      (run_literal_code, 'literal-code')],
                                     defaults=(run_script, 'script'))
_fuzz_prof_mp_markers = (
    (_fuzz_prof_mp_run_func
     + _Params.new(('prof_child_procs', 'label2'),
                   [(True, 'with-child-prof'), (False, 'no-child-prof')])
     + _get_mp_start_method_fuzzer(None))
    # Test all `multiproc` start methods with both locally- and
    # externally-defined profiling targets
    * (_Params.new(('preimports', 'label3'), [(False, 'no-preimports')])
       + _Params.new(('use_local_func', 'label4'),
                     [(True, 'local'), (False, 'external')],
                     defaults=(False, 'external')))
    # The 'with-preimports' case is already tested rather thoroughly in
    # `test_apply_mp_patches()`, so exclude these from the above "main"
    # param matrix and just test the different `kernprof` modes via the
    # `run_func()`s
    + (_fuzz_prof_mp_run_func
       + _Params.new(('preimports', 'label3'), [(True, 'with-preimports')]))
).sorted().split_on_params('fail')


def _test_profiling_multiproc_script(
    run_func: Callable[..., subprocess.CompletedProcess],
    request: pytest.FixtureRequest,
    test_module: _ModuleFixture,
    ext_module: _ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    prof_child_procs: bool,
    preimports: bool,
    use_local_func: bool,
    fail: bool,
    start_method: Literal['fork', 'forkserver', 'spawn'] | None,
    nnums: int,
    nprocs: int,
) -> None:
    # How many calls do we expect?
    nhits = dict.fromkeys(
        ['EXT-INVOCATION', 'EXT-LOOP', 'LOCAL-INVOCATION', 'LOCAL-LOOP'], 0,
    )
    # Make sure we're profiling the right function
    tag = 'LOCAL' if use_local_func else 'EXT'
    tag_call = tag + '-INVOCATION'
    tag_loop = tag + '-LOOP'
    if not fail:
        # The final sum in the parent process should always be profiled
        # unless the child processes failed and we never returned from
        # `Pool.starmap()`
        nhits[tag_call] += 1
        nhits[tag_loop] += nprocs
    if prof_child_procs:
        # When profiling extends into child processes, each of them
        # invokes the sum function once and when combined they loop thru
        # all the items
        nhits[tag_call] += nprocs
        nhits[tag_loop] += nnums

    runner = ['kernprof', '-l']
    runner.extend([
        '--{}prof-child-procs'.format('' if prof_child_procs else 'no-'),
        '--{}preimports'.format('' if preimports else 'no-'),
    ])
    if not use_local_func:
        # Also make sure to include the external module in `--prof-mod`
        runner.append(f'--prof-mod={ext_module.name}')
    run_func(
        request, test_module, tmp_path_factory,
        runner=runner,
        outfile='out.lprof',
        profile=True,
        use_local_func=use_local_func,
        fail=fail,
        start_method=start_method,
        nhits=nhits,
        nnums=nnums,
        nprocs=nprocs,
        timeout=_TEST_TIMEOUT,
        debug_log=(
            'debug.log' if prof_child_procs and _DEBUG else None
        ),
        subproc=False,
    )


@(_fuzz_prof_mp_markers[False])
@pytest.mark.parametrize(
    # XXX: should we explicitly test the single-proc case? We already
    # have quite a lot of subtests tho...
    ('nnums', 'nprocs'), [(2000, 3)],
)
def test_profiling_multiproc_script_success(
    run_func: Callable[..., subprocess.CompletedProcess],
    request: pytest.FixtureRequest,
    test_module: _ModuleFixture,
    ext_module: _ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    prof_child_procs: bool,
    preimports: bool,
    use_local_func: bool,
    start_method: Literal['fork', 'forkserver', 'spawn'] | None,
    nnums: int,
    nprocs: int,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str, label3: str, label4: str,
) -> None:
    """
    Check that `kernprof` can PROFILE the test module in various
    contexts when the parallel workload runs without errors, optionally
    extending profiling into child processes.

    Note:
        This test function is heavily parametrized. Here is why that is
        necessary:

        - ``run_func`` tests the different :cmd:`kernprof` modes (see
          :py:func:`~.test_running_multiproc_script`).

        - ``preimports`` tests that both mechanisms for setting up
          profiling targets work:

          - :py:const:`True`: child processes import the module
            generated by
            :py:mod:`line_profiler.autoprofile.eager_preimports`, like
            the main :py:mod:`kernprof` process does.

          - :py:const:`False`: child processes rewrite the executed code
            before passing it to :py:mod:`runpy`, similar to what
            :py:mod:`line_profiler.autoprofile.autoprofile` does.

          These code paths go through different
          :py:mod:`multiprocessing` components that we have patched and
          thus needs separate testing.

        - ``use_local_func`` tests that we can consistently set up
          profiling in both functions locally-defined in the profiled
          code and imported by it.

        - ``fail`` tests that our patches and hook doesn't choke when
          exceptions occur in child processes, and profiling data can
          still be collected.

        - ``start_method`` tests whether all available
          :py:mod:`multiprocessing` start methods are covered.

        - ``prof_child_procs`` of course toggles whether to do the
          patches to set up profiling in child processes.

    See also:
        :py:func:`test_profiling_multiproc_script_failure`
    """
    _test_profiling_multiproc_script(
        run_func=run_func,
        request=request,
        test_module=test_module,
        ext_module=ext_module,
        tmp_path_factory=tmp_path_factory,
        prof_child_procs=prof_child_procs,
        preimports=preimports,
        use_local_func=use_local_func,
        fail=False,
        start_method=start_method,
        nnums=nnums,
        nprocs=nprocs,
    )


@(_fuzz_prof_mp_markers[True])
@pytest.mark.parametrize(('nnums', 'nprocs'), [(2000, 3)])
def test_profiling_multiproc_script_failure(
    run_func: Callable[..., subprocess.CompletedProcess],
    request: pytest.FixtureRequest,
    test_module: _ModuleFixture,
    ext_module: _ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    prof_child_procs: bool,
    preimports: bool,
    use_local_func: bool,
    start_method: Literal['fork', 'forkserver', 'spawn'] | None,
    nnums: int,
    nprocs: int,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str, label3: str, label4: str,
) -> None:
    """
    Check that `kernprof` can PROFILE the test module in various
    contexts when the parallel workload errors out, optionally
    extending profiling into child processes.

    See also:
        :py:func:`test_profiling_multiproc_script_success`
    """
    _test_profiling_multiproc_script(
        run_func=run_func,
        request=request,
        test_module=test_module,
        ext_module=ext_module,
        tmp_path_factory=tmp_path_factory,
        prof_child_procs=prof_child_procs,
        preimports=preimports,
        use_local_func=use_local_func,
        fail=True,
        start_method=start_method,
        nnums=nnums,
        nprocs=nprocs,
    )


_fuzz_bare = (
    _Params.new(('use_subprocess', 'label1'),
                [(True, 'subprocess.run'), (False, 'os.system')])
    * _Params.new(('prof_child_procs', 'label2'),
                  [(True, 'with-child-prof'), (False, 'no-child-prof')])
    * _Params.new('n', [200])
)


def _test_profiling_bare_python(
    tmp_path_factory: pytest.TempPathFactory,
    ext_module: _ModuleFixture,
    use_subprocess: bool,
    prof_child_procs: bool,
    fail: bool,
    n: int,
) -> None:
    ext_module.install(children=True)
    temp_dir = tmp_path_factory.mktemp('mytemp')

    script_path = temp_dir / 'my-script.py'
    script_content = strip("""
    from {EXT_MODULE} import my_external_sum


    if __name__ == '__main__':
        numbers = list(range(1, 1 + {N}))
        result = my_external_sum(numbers, {FAIL})
    """.format(
        EXT_MODULE=ext_module.name,
        N=n,
        FAIL=fail,
    ))
    script_path.write_text(script_content)

    out_file = temp_dir / 'out.lprof'
    debug_log_file = temp_dir / 'debug.log'
    write_debug = _DEBUG and prof_child_procs
    cmd = [
        'kernprof', '-lv', '--preimports',
        f'--prof-mod={ext_module.name}',
        f'--outfile={out_file}',
        '--{}prof-child-procs'.format('' if prof_child_procs else 'no-'),
    ]
    if write_debug:
        cmd.append(f'--debug-log={debug_log_file}')
    sub_cmd = [sys.executable, str(script_path)]
    if use_subprocess:
        code = strip(f"""
        import subprocess


        subprocess.run({sub_cmd!r}, check=True)
        """)
    else:
        code = strip("""
        import os


        if os.system({!r}):
            raise RuntimeError('called process failed')
        """.format(concat_command_line(sub_cmd)))
    cmd.extend(['-c', code])
    proc = _run_subproc(
        cmd, text=True, capture_output=True, timeout=_TEST_TIMEOUT,
    )

    nhits = {'EXT-INVOCATION': 1, 'EXT-LOOP': n}
    if not prof_child_procs:
        for k in nhits:
            nhits[k] = 0

    try:
        # Check that the code errors out when expected
        assert bool(fail) == bool(proc.returncode)
        # Check that the profiling output is as expected
        for tag, num in nhits.items():
            _check_output(proc.stdout, tag, num)
    finally:
        if write_debug:
            print('-- Combined debug logs --', file=sys.stderr)
            print(
                indent(debug_log_file.read_text(), '  '),
                end='', file=sys.stderr,
            )
            print('-- End of debug logs --', file=sys.stderr)


@_fuzz_bare
def test_profiling_bare_python_success(
    tmp_path_factory: pytest.TempPathFactory,
    ext_module: _ModuleFixture,
    use_subprocess: bool,
    prof_child_procs: bool,
    n: int,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str,
) -> None:
    """
    Check that `kernprof` can profile the target functions if the code
    invokes another bare Python process (via either :py:func:`os.system`
    or :py:func:`subprocess.run`) that calls them and exits without
    errors.

    See also:
        :py:func:`test_profiling_bare_python_failure`
    """
    _test_profiling_bare_python(
        tmp_path_factory=tmp_path_factory,
        ext_module=ext_module,
        use_subprocess=use_subprocess,
        prof_child_procs=prof_child_procs,
        fail=False,
        n=n,
    )


@_fuzz_bare
def test_profiling_bare_python_failure(
    tmp_path_factory: pytest.TempPathFactory,
    ext_module: _ModuleFixture,
    use_subprocess: bool,
    prof_child_procs: bool,
    n: int,
    label1: str,
    label2: str,
) -> None:
    """
    Check that `kernprof` can profile the target functions if the code
    invokes another bare Python process (via either :py:func:`os.system`
    or :py:func:`subprocess.run`) that calls them and exits with errors.

    See also:
        :py:func:`test_profiling_bare_python_success`
    """
    _test_profiling_bare_python(
        tmp_path_factory=tmp_path_factory,
        ext_module=ext_module,
        use_subprocess=use_subprocess,
        prof_child_procs=prof_child_procs,
        fail=True,
        n=n,
    )
