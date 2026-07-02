from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Collection, Generator
from functools import partial
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import indent
from types import ModuleType
from typing import Any

import pytest

from line_profiler._child_process_profiling.cache import LineProfilingCache
from line_profiler.curated_profiling import (
    CuratedProfilerContext, ClassifiedPreimportTargets,
)
from line_profiler.autoprofile.util_static import _static_parse
from line_profiler.line_profiler import LineProfiler

from ._test_child_procs_utils import (
    preserve_object_attrs, ModuleFixture, StartMethod, ResultMismatch,
)


__all__ = (
    'ext_module', 'pool_test_module', 'pool_test_module_clone',
    'process_test_module',
    'ext_module_object', 'pool_test_module_object',
    'process_test_module_object',
    'create_cache', 'curated_profiler', 'another_pid',
)

# ========================== Module fixtures ===========================

# Only write the files once per test session

_EXAMPLES_PATH = Path(__file__).parent / 'multiproc_examples'


@pytest.fixture(scope='session')
def _ext_module() -> Generator[Path, None, None]:
    name = next(ModuleFixture.propose_name('my_ext_module'))
    with TemporaryDirectory() as mydir_str:
        my_dir = Path(mydir_str)
        my_dir.mkdir(exist_ok=True)
        my_module = my_dir / f'{name}.py'
        shutil.copy(_EXAMPLES_PATH / 'external_module.py', my_module)
        yield my_module


@pytest.fixture(scope='session')
def _pool_test_module(_ext_module: Path) -> Generator[Path, None, None]:
    yield from _dependent_module_path(
        _ext_module, 'pool_test_module.py', 'my_pool_test_module',
    )


@pytest.fixture(scope='session')
def _process_test_module(_ext_module: Path) -> Generator[Path, None, None]:
    yield from _dependent_module_path(
        _ext_module, 'process_test_module.py', 'my_process_test_module',
    )


def _dependent_module_path(
    _ext_module: Path, basename: str, module_name: str,
) -> Generator[Path, None, None]:
    name = next(ModuleFixture.propose_name(module_name))
    body = (_EXAMPLES_PATH / basename).read_text()
    body = body.replace('external_module', _ext_module.stem)
    with TemporaryDirectory() as mydir_str:
        my_dir = Path(mydir_str)
        my_dir.mkdir(exist_ok=True)
        my_module = my_dir / f'{name}.py'
        my_module.write_text(body)
        yield my_module


def _build_command_line(
    fail: bool, start_method: StartMethod | None, *,
    use_local_func: bool = False,
    nnums: int | None = None,
    nprocs: int | None = None,
) -> list[str]:
    args: list[str] = []
    if fail:
        args.append('--force-failure')
    if start_method:
        args.extend(['-s', start_method])
    if use_local_func:
        args.append('--local')
    if nnums:
        args.extend(['-l', str(nnums)])
    if nprocs:
        args.extend(['-n', str(nprocs)])
    return args


def _get_output(nnums_: int, /) -> Callable[..., int]:
    def get_output(nnums: int | None = None, **_) -> int:
        if nnums is None:
            nnums = nnums_
        return nnums * (nnums + 1) // 2

    return get_output


@pytest.fixture
def ext_module(
    _ext_module: Path, monkeypatch: pytest.MonkeyPatch,
) -> Generator[ModuleFixture, None, None]:
    """
    Yields:
        :py:class:`ModuleFixture` helper object containing the code at
        ``./multiproc_examples/external_module.py``
    """
    yield ModuleFixture(_ext_module, monkeypatch)


@pytest.fixture
def pool_test_module(
    _pool_test_module: Path,
    ext_module: ModuleFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[ModuleFixture, None, None]:
    """
    Yields:
        :py:class:`ModuleFixture` helper object containing the code at
        ``./multiproc_examples/pool_test_module.py``
    """
    yield from _yield_test_module(_pool_test_module, ext_module, monkeypatch)


@pytest.fixture
def pool_test_module_clone(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    pool_test_module: ModuleFixture,
    ext_module: ModuleFixture,
) -> Generator[ModuleFixture, None, None]:
    """
    Yields:
        :py:class:`ModuleFixture` helper object containing the same
        code as :py:data:`pool_test_module`
    """
    tmpdir = tmp_path_factory.mktemp('my_path')
    name = next(ModuleFixture.propose_name('my_cloned_pool_test_module'))
    path = tmpdir / f'{name}.py'
    path.write_text(pool_test_module.path.read_text())
    yield ModuleFixture(
        path, monkeypatch, [ext_module],
        pool_test_module.build_command_line, pool_test_module.get_output,
    )


@pytest.fixture
def process_test_module(
    _process_test_module: Path,
    ext_module: ModuleFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[ModuleFixture, None, None]:
    """
    Yields:
        :py:class:`ModuleFixture` helper object containing the code at
        ``./multiproc_examples/process_test_module.py``
    """
    yield from _yield_test_module(
        _process_test_module, ext_module, monkeypatch,
    )


def _yield_test_module(
    test_module: Path,
    ext_module: ModuleFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[ModuleFixture, None, None]:
    default_nnums = _static_parse('NUM_NUMBERS', test_module)
    assert isinstance(default_nnums, int)
    yield ModuleFixture(
        test_module, monkeypatch, [ext_module],
        _build_command_line, _get_output(default_nnums),
    )


@pytest.fixture
def ext_module_object(
    ext_module: ModuleFixture,
) -> Generator[ModuleType, None, None]:
    """
    Yields:
        :py:class:`ModuleType` object containing the code at
        ``./multiproc_examples/external_module.py``, and is torn down at
        the end of the test
    """
    yield from ext_module._import_module_helper()


@pytest.fixture
def pool_test_module_object(
    pool_test_module: ModuleFixture, ext_module_object: ModuleType,
) -> Generator[ModuleType, None, None]:
    """
    Yields:
        :py:class:`ModuleType` object containing the code at
        ``./multiproc_examples/pool_test_module.py``, and is torn down
        at the end of the test
    """
    yield from pool_test_module._import_module_helper()


@pytest.fixture
def process_test_module_object(
    process_test_module: ModuleFixture, ext_module_object: ModuleType,
) -> Generator[ModuleType, None, None]:
    """
    Yields:
        :py:class:`ModuleType` object containing the code at
        ``./multiproc_examples/process_test_module.py``, and is torn
        down at the end of the test
    """
    yield from process_test_module._import_module_helper()


# =========================== Misc. fixtures ===========================


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
        with preserve_object_attrs(LineProfilingCache, ['_loaded_instance']):
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
