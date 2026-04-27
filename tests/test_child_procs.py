from __future__ import annotations

import ast
import dataclasses
import enum
import inspect
import multiprocessing.pool
import operator
import os
import re
import shlex
import subprocess
import sys
import sysconfig
from abc import ABC, abstractmethod
from collections.abc import (
    Callable, Collection, Generator, Iterable, Mapping, Sequence,
)
from contextlib import ExitStack
from functools import lru_cache, partial, wraps
from io import StringIO
from importlib import import_module
from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory
from textwrap import dedent, indent
from time import monotonic
from types import TracebackType
from typing import Any, Generic, Literal, TypeVar, cast, final, overload
from typing_extensions import Self, ParamSpec
from uuid import uuid4

import pytest
import ubelt as ub

from line_profiler._child_process_profiling.cache import LineProfilingCache
from line_profiler._child_process_profiling.runpy_patches import (
    create_runpy_wrapper,
)
from line_profiler._child_process_profiling.multiprocessing_patches import (
    _Poller,
)
from line_profiler.curated_profiling import (
    CuratedProfilerContext, ClassifiedPreimportTargets,
)
from line_profiler.line_profiler import LineProfiler, LineStats


T = TypeVar('T')
T1 = TypeVar('T1')
T2 = TypeVar('T2')
TCtx_ = TypeVar('TCtx_')
PS = ParamSpec('PS')
C = TypeVar('C', bound=Callable[..., Any])

NUM_NUMBERS = 100
NUM_PROCS = 4
START_METHODS = set(multiprocessing.get_all_start_methods())

# XXX: owing to the shenanigans in
# `line_profiler._child_process_profiling.multiprocessing_patches`,
# there is a risk that failing child processes are not properly
# `.terminate()`-ed. So just put in a timeout...
_NUM_RETRIES = 2
_SUBPROC_TIMEOUT = 5  # Seconds
_DEBUG = True


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
from multiprocessing import get_context, Pool
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
    start_method: Literal['fork', 'forkserver', 'spawn'] | None = None,
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
    if start_method:
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
      to :py:const`True`, automatically creates an instance of
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


@final
class ResultMismatch(ValueError):
    def __init__(
        self,
        expected: Any,
        actual: Any | _NotSupplied = _NotSupplied.NOT_SUPPLIED,
        _trunc_tb: int = 0,
    ) -> None:
        msg = f'expected: {expected}'
        if actual != _NotSupplied.NOT_SUPPLIED:
            msg = f'{msg}, got {actual}'
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

    def __call__(self, func: C) -> C:
        """
        Mark a callable as with :py:func:`pytest.mark.parametrize`.
        """
        # Note: `pytest` automatically assumes single-param values to
        # be unpackes, so comply here
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
        self, obj: Any, attrs: Collection[str], debug: bool = _DEBUG,
    ) -> None:
        self.obj = obj
        self.attrs = set(attrs)
        self._callbacks: list[Callable[[], None]] = []
        self.debug = debug

    def __enter__(self) -> dict[str, Any]:
        def get_repr(attr: str) -> str:
            try:
                value = getattr(self.obj, attr)
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

        result: dict[str, Any] = {}
        for attr in self.attrs:
            old = getattr(self.obj, attr, _NotSupplied.NOT_SUPPLIED)
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
        self, targets: Mapping[str, Collection[str]], debug: bool = _DEBUG,
    ) -> None:
        self.targets = {
            target: set(attrs) for target, attrs in targets.items()
        }
        self._stacks: list[ExitStack] = []
        self.debug = debug

    def __enter__(self) -> dict[str, dict[str, Any]]:
        stack = ExitStack()
        self._stacks.append(stack)
        result: dict[str, Any] = {}
        for target, attrs in self.targets.items():
            result[target] = stack.enter_context(_preserve_obj_attributes(
                _import_target(target), attrs, debug=self.debug,
            ))
        return result

    def __exit__(self, *_, **__) -> None:
        self._stacks.pop().close()


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


@lru_cache()
def _find_return_lines(func: str) -> list[int]:
    class FindReturns(ast.NodeVisitor):
        def __init__(self) -> None:
            self.found: set[int] = set()

        def visit_Return(self, node: ast.Return) -> None:
            self.found.add(node.lineno)
            self.generic_visit(node)

    func_obj = _import_target(func)
    assert inspect.isfunction(func_obj)
    lines, start = inspect.getsourcelines(func_obj)
    tree = ast.parse(''.join(lines))
    finder = FindReturns()
    finder.visit(tree)
    return sorted(lineno + start - 1 for lineno in finder.found)


# `shlex.join()` doesn't work properly on Windows, so use
# `subprocess.list2cmdline()` instead;
# though an "intentionally" undocumented API (cpython issue #10308),
# it's been around since 2.4, seems stable enough, and does exactly what
# is needed
if sys.platform == 'win32':
    concat_command_line: Callable[
        [Sequence[str]], str
    ] = subprocess.list2cmdline
else:
    concat_command_line = shlex.join


def _run_as_script(
    runner_args: list[str], test_args: list[str], test_module: _ModuleFixture,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + [str(test_module.path)] + test_args
    test_module.install(children=True, deps_only=True)
    return _run_subproc(cmd, **kwargs)


def _run_as_module(
    runner_args: list[str], test_args: list[str], test_module: _ModuleFixture,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + ['-m', test_module.name] + test_args
    test_module.install(children=True)
    return _run_subproc(cmd, **kwargs)


def _run_as_literal_code(
    runner_args: list[str], test_args: list[str], test_module: _ModuleFixture,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + ['-c', test_module.path.read_text()] + test_args
    test_module.install(children=True, deps_only=True)
    return _run_subproc(cmd, **kwargs)


def _run_subproc(
    cmd: Sequence[str] | str,
    /,
    *args,
    check: bool = False,
    env: Mapping[str, str] | None = None,
    **kwargs
) -> subprocess.CompletedProcess:
    """
    Wrapper around :py:func:`subprocess.run` which writes debugging
    output.
    """
    if isinstance(cmd, str):
        cmd_str = cmd
    else:
        cmd_str = concat_command_line(cmd)

    # If we're capturing outputs, it may be for the best to wait until
    # we've processed the output streams to check the return code...
    check_rc_in_run = check
    for arg in 'stdout', 'stdin':
        if kwargs.get(arg) not in {None, subprocess.DEVNULL}:
            check_rc_in_run = False
    if kwargs.get('capture_output'):
        check_rc_in_run = False

    print('Command:', cmd_str)
    if env is not None:
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
    print('-- Process start --')
    # Note: somehow `mypy` doesn't agree with simply unpacking the
    # `*args` into `subprocess.run()`...
    status: int | str = '???'
    proc: subprocess.CompletedProcess | None = None
    time = monotonic()
    try:
        proc = subprocess.run(  # type: ignore[call-overload]
            cmd, *args, env=env, check=check_rc_in_run, **kwargs,
        )
    except Exception:
        status = 'error'
        raise
    else:
        assert proc is not None
        if check and not check_rc_in_run:  # Perform missing check
            proc.check_returncode()
        status = proc.returncode
        return proc
    finally:
        time = monotonic() - time
        if proc is not None:
            captured: str | bytes | None
            for name, captured, stream in [
                ('stdout', proc.stdout, sys.stdout),
                ('stderr', proc.stderr, sys.stderr),
            ]:
                if captured is None:
                    continue
                if isinstance(captured, bytes):  # `text=False`
                    captured = captured.decode()
                print(f'{name}:\n{indent(captured, "  ")}', file=stream)
        print(
            f'-- Process end (time elapsed: {time:.2f} s / '
            f'return status: {status})--'
        )


def _run_test_module(
    run_helper: Callable[..., subprocess.CompletedProcess],
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
        proc = run_helper(
            runner_args, test_args, test_module,
            text=True, capture_output=True, check=(check and not fail),
            **kwargs
        )
        try:
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
            # - Temporary `.pth` file(s) created by `~~.pth_hook` has
            #   been cleaned up
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
            if debug_log is not None:
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

# ============================= Unit tests =============================

# XXX: Tests in this section concerns implementation details, and the
# tested APIs and behaviors MUST NOT be relied upon by end-users.

_GLOBAL_PATCHES = {
    'multiprocessing.process.BaseProcess': frozenset({
        '_bootstrap', 'terminate',
    }),
    'multiprocessing.spawn': frozenset({'runpy'}),
    'os': frozenset({'fork'}),
}

# NOTE: we need a function which isn't used by the codebase itself
# (esp. during cache cleanup); otherwise the profiling results may
# be skewed
_SAFE_TARGET = 'calendar.weekday'
_SAFE_TARGET_ARGS = [
    (1970, 1, 1),
    (2000, 12, 31),
    (2008, 9, 16),  # Where the repo started
]


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
    envvars: set[str] = set(os.environ)
    try:
        original.inject_env_vars()  # Needed for `.load()`
        try:
            # Env vars should be inserted
            assert set(os.environ) == envvars.union(original.environ) > envvars
            original.dump()
            loaded = original.load()
            reloaded = original.load()
            assert original is not loaded is reloaded
            # Compare init fields
            for field in dataclasses.fields(LineProfilingCache):
                if not field.init:
                    continue
                assert (
                    getattr(original, field.name)
                    == getattr(loaded, field.name)
                )
        finally:  # Explicitly cleanup
            original.cleanup()
    finally:  # Env vars restored after cleanup
        assert set(os.environ) == envvars


@pytest.mark.parametrize(('wrap_os_fork', 'label1'),
                         [(True, 'with-wrap-fork'), (False, 'no-wrap-fork')])
@pytest.mark.parametrize(('debug', 'label2'),
                         [(True, 'with-debug'), (False, 'no-debug')])
def test_cache_setup_main_process(
    create_cache: Callable[..., LineProfilingCache],
    wrap_os_fork: bool,
    debug: bool,
    label1: str, label2: str,
) -> None:
    """
    Test that :py:meth:`LineProfilingCache._setup_in_main_process` works
    as expected.
    """
    cache = create_cache(debug=debug)
    patches: dict[str, dict[str, bool]] = {
        target: dict.fromkeys(attrs, True)
        for target, attrs in _GLOBAL_PATCHES.items()
    }
    patches['os']['fork'] = wrap_os_fork and (sys.platform != 'win32')
    targets: dict[str, Any] = {
        target: _import_target(target) for target in patches
    }
    with ExitStack() as stack:
        patched = stack.enter_context(_preserve_attributes(patches))
        original_pths = stack.enter_context(_preserve_pth_files())
        cache._setup_in_main_process(wrap_os_fork=wrap_os_fork)
        # There should be exactly one extra `.pth` file
        new_pth_hook, = _preserve_pth_files.get_pth_files() - original_pths
        # Check whether the patches are applied
        for target, maybe_patches in patches.items():
            obj = targets[target]
            for attr, is_patched in maybe_patches.items():
                orig_value = patched[target][attr]
                if orig_value is _NotSupplied.NOT_SUPPLIED:
                    assert not hasattr(obj, attr)
                else:
                    assert (getattr(obj, attr) is orig_value) != is_patched
        # Check whether the patches are reversed
        cache.cleanup()
        for target, orig_attrs in patched.items():
            obj = targets[target]
            for attr, orig_value in orig_attrs.items():
                if orig_value is _NotSupplied.NOT_SUPPLIED:
                    assert not hasattr(obj, attr)
                else:
                    assert getattr(obj, attr) is orig_value
        # Check that the instance is set as the `.load()`-ed one
        assert cache is cache.load()

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
@_preserve_attributes(_GLOBAL_PATCHES)
def test_cache_setup_child(
    create_cache: Callable[..., LineProfilingCache],
    curated_profiler: LineProfiler,
    wrap_os_fork: bool,
    preimports: bool,
    new_profiler: bool,
    debug: bool,
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

    # Make sure we get a different PID from the current process
    curr_pid = os.getpid()
    main_pid = (curr_pid - 42) % (2 * 16)
    assert main_pid != curr_pid

    cache = create_cache(
        profiling_targets=[_SAFE_TARGET],
        preimports_module=preimports,
        _use_curated_profiler=not new_profiler,
        main_pid=main_pid,
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
        _import_target(_SAFE_TARGET)(*_SAFE_TARGET_ARGS[0])
        stats = cache.profiler.get_stats()
        for callback, has_prof_data, fork_patched in [
            (lambda: None, False, wrap_os_fork),
            (cache.cleanup, preimports, False),
        ]:
            callback()
            gathered = cache.gather_stats()
            assert any(gathered.timings.values()) == has_prof_data, gathered
            if hasattr(os, 'fork'):
                assert (os.fork is not old_fork) == fork_patched
            else:  # E.g. Windows
                assert old_fork == _NotSupplied.NOT_SUPPLIED

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


@pytest.mark.retry(_NUM_RETRIES, exceptions=(ResultMismatch, _Poller.Timeout))
@pytest.mark.parametrize('start_method',
                         ['fork', 'forkserver', 'spawn', 'dummy'])
@pytest.mark.parametrize(('debug', 'label'),
                         [(True, 'with-debug'), (False, 'no-debug')])
@_preserve_pth_files()
@_preserve_attributes(_GLOBAL_PATCHES)
def test_apply_mp_patches(
    tmp_path_factory: pytest.TempPathFactory,
    create_cache: Callable[..., LineProfilingCache],
    start_method: Literal['fork', 'forkserver', 'spawn', 'dummy'],
    debug: bool,
    label: str,
) -> None:
    """
    Test that :py:func:`line_profiler._child_process_profiling\
.multiprocessing_patches.apply`
    works as expected.
    """
    def is_valid_stats_file(path: os.PathLike[str] | str) -> bool:
        try:
            LineStats.from_files(path, on_defective='error')
        except Exception:
            return False
        return True

    config: Path | None = None
    if debug:
        config = tmp_path_factory.mktemp('myconfig') / 'mytoml.toml'
        config.write_text(
            '[tool.line_profiler.child_processes.multiprocessing]\n'
            'intercept_logs = true'
        )

    cache = create_cache(
        profiling_targets=[_SAFE_TARGET],
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

    func = _import_target(_SAFE_TARGET)
    return_lines = _find_return_lines(_SAFE_TARGET)
    Pool: Callable[..., multiprocessing.pool.Pool]
    if start_method == 'dummy':
        Pool = _import_target('multiprocessing.dummy.Pool')
        # Twice the counted calls because we're also collecting the
        # checking calls in this process
        expected_ncalls = len(_SAFE_TARGET_ARGS) * 2
        get_stats: Callable[[], LineStats] = cache.profiler.get_stats
    elif start_method not in START_METHODS:
        pytest.skip(
            f'`multiprocessing` start method {start_method!r} '
            'not available on the platform'
        )
    else:
        Pool = multiprocessing.get_context(start_method).Pool
        expected_ncalls = len(_SAFE_TARGET_ARGS)
        get_stats = cache.gather_stats

    with Pool(2) as pool:
        par_result = pool.starmap(func, _SAFE_TARGET_ARGS)
        pool.close()
        pool.join()
    assert par_result == [func(*args) for args in _SAFE_TARGET_ARGS]

    # Check that calls in children are traced
    line_entries = get_stats().timings[
        inspect.getfile(func), inspect.getsourcelines(func)[1], func.__name__,
    ]
    num_returns = sum(
        nhits for lineno, nhits, _ in line_entries if lineno in return_lines
    )
    ResultMismatch.compare(expected_ncalls, num_returns)

    # Check the debug logs to see if we have done everything right, esp.
    # the logging interception part not covered by other tests
    patterns: dict[str, bool] = {
        'Cleanup succeeded.*: .*dump_stats.*' + re.escape(path.name): True
        for path in Path(cache.cache_dir).glob('*.lprof')
        if is_valid_stats_file(path)
    }
    patterns[re.escape('`multiprocessing` logging (debug)')] = debug
    _search_cache_logs(cache, True, patterns)


# XXX: End of tests for implementation details

# ========================= Integration tests ==========================


def _get_mp_start_method_fuzzer(label_name: str) -> _Params:
    """
    Returns:
        :py:class:`_Params` object which does a full Cartesian-product
        fuzz between ``fail`` (true or false) and ``start_method``
        ('fork', 'forkserver', and 'spawn'; default :py:const:`None`)
    """
    fuzz_fail = _Params.new(('fail', label_name),
                            [(True, 'failure'), (False, 'success')],
                            defaults=(False, 'success'))
    fuzz_start = _Params.new('start_method', ['fork', 'forkserver', 'spawn'],
                             defaults=None)
    return fuzz_fail * fuzz_start


_fuzz_sanity = (
    _Params.new(('run_func', 'label1'),
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
                  defaults=(None, None))
)


@_fuzz_sanity
def test_multiproc_script_sanity_check(
    run_func: Callable[..., subprocess.CompletedProcess],
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
        test_module, tmp_path_factory,
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
    run_func(test_module, tmp_path_factory, runner, outfile, profile)


_fuzz_prof_mp_1 = (
    _Params.new(('run_func', 'label1'),
                [(run_module, 'module'),
                 (run_script, 'script'),
                 (run_literal_code, 'literal-code')],
                defaults=(run_script, 'script'))
    + _Params.new(('prof_child_procs', 'label2'),
                  [(True, 'with-child-prof'), (False, 'no-child-prof')])
    + _get_mp_start_method_fuzzer('label3')
)
_fuzz_prof_mp_2 = (
    _Params.new(('preimports', 'label4'),
                [(True, 'with-preimports'), (False, 'no-preimports')],
                defaults=(False, 'no-preimports'))
    + _Params.new(('use_local_func', 'label5'),
                  [(True, 'local'), (False, 'external')],
                  defaults=(False, 'external'))
)


@_fuzz_prof_mp_1
@_fuzz_prof_mp_2
@pytest.mark.retry(_NUM_RETRIES,
                   exceptions=(ResultMismatch, subprocess.TimeoutExpired))
@pytest.mark.parametrize(
    # XXX: should we explicitly test the single-proc case? We already
    # have quite a lot of subtests tho...
    ('nnums', 'nprocs'), [(2000, 3)],
)
def test_profiling_multiproc_script(
    run_func: Callable[..., subprocess.CompletedProcess],
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
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str, label3: str, label4: str, label5: str,
) -> None:
    """
    Check that `kernprof` can PROFILE the test module in various
    contexts, optionally extending profiling into child processes.

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
    """
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
        test_module, tmp_path_factory,
        runner=runner,
        outfile='out.lprof',
        profile=True,
        use_local_func=use_local_func,
        fail=fail,
        start_method=start_method,
        nhits=nhits,
        nnums=nnums,
        nprocs=nprocs,
        timeout=_SUBPROC_TIMEOUT,
        debug_log=(
            'debug.log' if prof_child_procs and _DEBUG else None
        ),
    )


@pytest.mark.retry(_NUM_RETRIES,
                   exceptions=(ResultMismatch, subprocess.TimeoutExpired))
@pytest.mark.parametrize(('use_subprocess', 'label1'),
                         [(True, 'subprocess.run'), (False, 'os.system')])
@pytest.mark.parametrize(('prof_child_procs', 'label2'),
                         [(True, 'with-child-prof'), (False, 'no-child-prof')])
@pytest.mark.parametrize(('fail', 'label3'),
                         [(True, 'failure'), (False, 'success')])
@pytest.mark.parametrize('n', [200])
def test_profiling_bare_python(
    tmp_path_factory: pytest.TempPathFactory,
    ext_module: _ModuleFixture,
    use_subprocess: bool,
    prof_child_procs: bool,
    fail: bool,
    n: int,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str, label3: str,
) -> None:
    """
    Check that `kernprof` can profile the target functions if the code
    invokes another bare Python process (via either :py:func:`os.system`
    or :py:func:`subprocess.run`) that calls them.
    """
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
        cmd, text=True, capture_output=True, timeout=_SUBPROC_TIMEOUT,
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
