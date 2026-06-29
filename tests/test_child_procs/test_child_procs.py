from __future__ import annotations

import dataclasses
import inspect
import operator
import os
import re
import sys
from collections.abc import Callable, Collection, Iterable
from contextlib import ExitStack
from functools import partial
from io import StringIO
from pathlib import Path
from runpy import run_path
from subprocess import CompletedProcess
from textwrap import indent
from types import ModuleType
from typing import cast

import pytest

from _line_profiler_hooks import load_pth_hook
from line_profiler._child_process_profiling.cache import LineProfilingCache
from line_profiler._child_process_profiling.runpy_patches import (
    create_runpy_wrapper,
)
from line_profiler._child_process_profiling.multiprocessing_patches import (
    MPConfig, _PATCHES as MP_PATCHES,
)
from line_profiler.line_profiler import LineStats
from line_profiler.toml_config import ConfigSource

from ._test_child_procs_utils import (
    DEBUG, DEFAULT_TIMEOUT, NOT_SUPPLIED, START_METHODS, PATCH_SUMMARIES,
    ModuleFixture, Params, ResultMismatch, StartMethod,
    preserve_object_attrs, preserve_targets,
    cleanup_extra_pth_files, add_timeout,
    CheckWarnings,
    mp_patch_is_internal, get_mp_patches_toml_text,
    summarize_mp_patches, filter_mp_patch_summary,
    concat_command_line, run_subproc,
    run_module, run_script, run_literal_code, check_tagged_line_nhits,
    strip, search_cache_logs,
)


# ============================= Unit tests =============================

# XXX: Tests in this section concern implementation details, and the
# tested APIs and behaviors MUST NOT be relied upon by end-users.

_DEFAULT_MP_CONFIG = MPConfig.from_config(ConfigSource.from_default())


@pytest.mark.parametrize(('run_profiled_code', 'label1'),
                         [(True, 'run-profiled'), (False, 'run-unrelated')])
@pytest.mark.parametrize(('as_module', 'label2'),
                         [(True, 'run_module'), (False, 'run_path')])
@pytest.mark.parametrize(('debug', 'label3'),
                         [(True, 'with-debug'), (False, 'no-debug')])
def test_runpy_patches(
    capsys: pytest.CaptureFixture[str],
    ext_module: ModuleFixture,
    pool_test_module: ModuleFixture,
    pool_test_module_clone: ModuleFixture,
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
        rewrite_module=pool_test_module.path,
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
        module = pool_test_module
        num_invocations, num_loops = 1, nprocs
        expected_funcs: set[str] = {'my_external_sum', 'split_workload'}
    else:
        module = pool_test_module_clone
        num_invocations, num_loops = 0, 0
        expected_funcs = set()
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
    funcs = {func.__name__ for func in getattr(cache.profiler, 'functions')}
    assert funcs == expected_funcs

    # Check that calls in the current process are profiled iif the
    # correct file is executed
    with StringIO() as sio:
        cache.profiler.print_stats(sio)
        stats = sio.getvalue()
    check_tagged_line_nhits(stats, 'EXT-INVOCATION', num_invocations)
    check_tagged_line_nhits(stats, 'EXT-LOOP', num_loops)

    # Check the debug-log entries are correctly gathered
    search_cache_logs(
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


@(Params.new(('wrap_os_fork', 'label1'),
             [(True, 'with-wrap-fork'), (False, 'no-wrap-fork')])
  + Params.new(('debug', 'label2'),
               [(True, 'with-debug'), (False, 'no-debug')])
  + Params.new(('patch_pool', 'patch_process', 'intercept_logs', 'label3'),
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
    config.write_text(get_mp_patches_toml_text(mp_patches))
    cache = create_cache(debug=debug, config=config)

    # Check that only the requested patches are applied
    patches = summarize_mp_patches([
        (True, PATCH_SUMMARIES['minimal']),
        *(
            (name in mp_patches, filter_mp_patch_summary(patch.summary))
            for name, patch in MP_PATCHES.items()
            if not mp_patch_is_internal(name)
        ),
    ])
    try:
        patches['os']['fork'] = wrap_os_fork
    except KeyError:
        # `os.fork()` pruned because it doesn't exist on e.g. Windows
        assert not hasattr(os, 'fork')

    with ExitStack() as stack:
        patched = stack.enter_context(preserve_targets(patches))
        compare_patched = partial(
            preserve_targets.compare_with_current_values, patched,
        )
        original_pths = stack.enter_context(cleanup_extra_pth_files())
        cache._setup_in_main_process(wrap_os_fork=wrap_os_fork)
        # There should be exactly one extra `.pth` file
        new_pth_hook, = cleanup_extra_pth_files.get_pth_files() - original_pths
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
    search_cache_logs(cache, debug, patterns)


@pytest.mark.parametrize(('wrap_os_fork', 'label1'),
                         [(True, 'with-wrap-fork'), (False, 'no-wrap-fork')])
@pytest.mark.parametrize(('preimports', 'label2'),
                         [(True, 'with-preimports'), (False, 'no-preimports')])
@pytest.mark.parametrize(('new_profiler', 'label3'),
                         [(True, 'no-profiler'), (False, 'with-profiler')])
@pytest.mark.parametrize(('debug', 'label4'),
                         [(True, 'with-debug'), (False, 'no-debug')])
@pytest.mark.parametrize('n', [100])
@preserve_targets()
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

    with preserve_object_attrs(os, ['fork']) as preserved:
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
            with CheckWarnings() as cw:
                handle_warning: list[Callable[..., None]] = []
                if has_nonempty_file:
                    handle_warning.append(cw.forbid_warnings)
                else:  # Check for the warning but don't reissue it
                    handle_warning.extend([
                        cw.expect_warnings, cw.suppress_warnings,
                    ])
                for handle in handle_warning:
                    handle(r'.* file\(s\) .* empty', module='line_profiler')
                gathered = cache.gather_stats()
            assert any(gathered.timings.values()) == has_stats, gathered
            if hasattr(os, 'fork'):
                assert (os.fork is not old_fork) == fork_patched
            else:  # E.g. Windows
                assert old_fork == NOT_SUPPLIED
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
    search_cache_logs(cache, debug, patterns)


@pytest.mark.parametrize('ppid_should_match', [True, False, None])
@preserve_targets()
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

    compare = preserve_targets.compare_with_current_values
    patches = {
        target: frozenset(attr for attr, patched in attrs.items() if patched)
        for target, attrs in summarize_mp_patches([
            (True, PATCH_SUMMARIES['default']),
            (True, PATCH_SUMMARIES['pth_hook']),
        ]).items()
    }
    with preserve_targets(patches) as patched:
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
            with preserve_targets(patches) as re_patched:
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


@cleanup_extra_pth_files()
@preserve_targets()
@add_timeout
def _test_apply_mp_patches_inner(
    tmp_path_factory: pytest.TempPathFactory,
    create_cache: Callable[..., LineProfilingCache],
    ext_module_object: ModuleType,
    test_module_object: ModuleType,
    start_method: StartMethod,
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
    assert {'pool', 'process'} & set(mp_patches)  # Needed for profiling
    cfg_chunks: list[str] = [
        get_mp_patches_toml_text(mp_patches),
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
    iter_stats: Iterable[Path] = Path(cache.cache_dir).glob('*.lprof')
    iter_stats = cast(  # See `ty` issue #3428
        Iterable[Path], filter(is_valid_stats_file, iter_stats),
    )
    pat = 'Cleanup succeeded.*: .*dump_stats.*{}'
    patterns.update({
        pat.format(re.escape(path.name)): True for path in iter_stats
    })
    logger_pat = '{} {}'.format(
        re.escape('`multiprocessing` logging'),
        r'\((sub_)?debug|info|sub_warning|warn\)',
    )
    patterns[logger_pat] = intercept_logs
    search_cache_logs(cache, True, patterns)


def _test_apply_mp_patches(
    patch_process: bool | None = None,
    patch_pool: bool | None = None,
    intercept_logs: bool | None = None,
    *,
    start_method: StartMethod,
    **kwargs
) -> None:
    if start_method not in ('dummy', *START_METHODS):
        pytest.skip(
            f'`multiprocessing` start method {start_method!r} '
            'not available on the platform'
        )

    patches = cast(dict[str, bool], _DEFAULT_MP_CONFIG.patches.copy())
    for name, applied in {
        'pool': patch_pool, 'process': patch_process,
        'logging': intercept_logs,
    }.items():
        if applied is not None:
            patches[name] = applied
    mp_patches = [name for name, applied in patches.items() if applied]

    with CheckWarnings() as cw:
        cw.forbid_warnings('.*resource_tracker', module='multiprocessing')
        cw.forbid_warnings(
            r'.* file\(s\) .* empty',
            category=UserWarning, module='line_profiler',
        )
        if start_method == 'fork':
            # The `@add_timeout` decorator spins up a new thread for
            # executing `_test_apply_mp_patches_pool_inner()`;
            # explicitly ignore the associated warning when we use
            # `start_method='fork'`
            cw.suppress_warnings(
                r'.*multi-?threaded.*fork\(\)', category=DeprecationWarning,
            )
        _test_apply_mp_patches_inner(
            mp_patches=mp_patches, start_method=start_method, **kwargs,
        )


@(Params.new('start_method', ['fork', 'forkserver', 'spawn', 'dummy'],
             defaults='dummy')
  # We only need to check if `intercept_logs = logging` work, the other
  # parametrizations don't matter
  + Params.new(('intercept_logs', 'label1'),
               [(True, 'with-logging'), (False, 'no-logging')],
               defaults=(None, 'default-logging'))).sorted()
@pytest.mark.parametrize(
    ('test_module', 'patch_process', 'patch_pool', 'label2'),
    [('pool_test_module', True, True, 'patch-pool-and-process'),
     ('pool_test_module', False, True, 'patch-pool-only'),
     ('process_test_module', True, True, 'patch-pool-and-process'),
     ('process_test_module', True, False, 'patch-process-only')])
@pytest.mark.parametrize(('n', 'nprocs'), [(100, 2)])
def test_apply_mp_patches_success(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    create_cache: Callable[..., LineProfilingCache],
    ext_module_object: ModuleType,
    test_module: str,
    start_method: StartMethod,
    patch_process: bool,
    patch_pool: bool,
    intercept_logs: bool | None,
    n: int,
    nprocs: int,
    label1: str,
    label2: str,
) -> None:
    """
    Test that :py:func:`line_profiler._child_process_profiling\
.multiprocessing_patches.apply`
    works as expected for Python code using parallelism based on
    :py:class:`multiprocessing.pool.Pool` and
    :py:class:`multiprocessing.process.BaseProcess`, when the parallel
    workload DOES NOT error out.

    See also:
        :py:func:`test_apply_mp_patches_failure`
    """
    test_module_object = request.getfixturevalue(test_module + '_object')
    assert isinstance(test_module_object, ModuleType)
    _test_apply_mp_patches(
        patch_process,
        patch_pool,
        intercept_logs,
        tmp_path_factory=tmp_path_factory,
        create_cache=create_cache,
        ext_module_object=ext_module_object,
        test_module_object=test_module_object,
        start_method=start_method,
        fail=False,
        n=n,
        nprocs=nprocs,
    )


@pytest.mark.parametrize('start_method',
                         ['fork', 'forkserver', 'spawn', 'dummy'])
@pytest.mark.parametrize(
    ('test_module', 'patch_process', 'patch_pool', 'label'),
    [('pool_test_module', True, True, 'patch-pool-and-process'),
     ('pool_test_module', False, True, 'patch-pool-only'),
     ('process_test_module', True, True, 'patch-pool-and-process'),
     ('process_test_module', True, False, 'patch-process-only')])
@pytest.mark.parametrize(('n', 'nprocs'), [(100, 2)])
def test_apply_mp_patches_failure(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    create_cache: Callable[..., LineProfilingCache],
    ext_module_object: ModuleType,
    test_module: str,
    start_method: StartMethod,
    patch_process: bool,
    patch_pool: bool,
    n: int,
    nprocs: int,
    label: str,
) -> None:
    """
    Test that :py:func:`line_profiler._child_process_profiling\
.multiprocessing_patches.apply`
    works as expected for Python code using parallelism based on
    :py:class:`multiprocessing.pool.Pool` and
    :py:class:`multiprocessing.process.BaseProcess`, when the parallel
    workload DOES error out.

    See also:
        :py:func:`test_apply_mp_patches_success`
    """
    test_module_object = request.getfixturevalue(test_module + '_object')
    assert isinstance(test_module_object, ModuleType)
    _test_apply_mp_patches(
        patch_process,
        patch_pool,
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


def _get_mp_start_method_fuzzer(label_name: str | None) -> Params:
    """
    Returns:
        :py:class:`Params` object which does a full Cartesian-product
        fuzz between ``fail`` (true or false) and ``start_method``
        ('fork', 'forkserver', and 'spawn'; default :py:const:`None`)
    """
    if label_name is None:
        label_name, drop_label = '_', True
    else:
        drop_label = False
    fuzz_fail = Params.new(('fail', label_name),
                           [(True, 'failure'), (False, 'success')],
                           defaults=(False, 'success'))
    if drop_label:
        fuzz_fail = fuzz_fail.drop_params(label_name)
    fuzz_start = Params.new('start_method', ['fork', 'forkserver', 'spawn'],
                            defaults=None)
    return fuzz_fail * fuzz_start


@(Params.new('test_module', ['pool_test_module', 'process_test_module'])
  * Params.new(('run_func', 'label1'),
               [(run_module, 'module'), (run_script, 'script')])
  * Params.new(('use_local_func', 'label2'),
               [(True, 'local'), (False, 'ext')])
  # Python can't pickle things unless they resided in a retrievable
  # location (so not the script supplied by `python -c`); this also
  # means that `process_test_module` cannot be `python -c`-ed at all,
  # because `Worker` is locally-defined
  + Params.new(
      ('test_module', 'run_func', 'label1', 'use_local_func', 'label2'),
      [('pool_test_module', run_literal_code, 'literal-code', False, 'ext')])
  # Also fuzz the parallelization-related stuff, esp. check what
  # happens if an exception is raised inside the parallelly-run func
  + _get_mp_start_method_fuzzer('label3')
  + Params.new(('nnums', 'nprocs'), [(200, None), (None, 3)],
               defaults=(None, None))).sorted()
def test_multiproc_script_sanity_check(
    run_func: Callable[..., CompletedProcess],
    request: pytest.FixtureRequest,
    test_module: str,
    tmp_path_factory: pytest.TempPathFactory,
    use_local_func: bool,
    fail: bool,
    start_method: StartMethod | None,
    nnums: int | None,
    nprocs: int | None,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str, label3: str,
) -> None:
    """
    Sanity check that the test modules function as expected when run
    with vanilla Python.
    """
    module_fixture = request.getfixturevalue(test_module)
    assert isinstance(module_fixture, ModuleFixture)
    run_func(
        request, module_fixture, tmp_path_factory,
        runner=sys.executable, profile=False,
        fail=fail,
        start_method=start_method,
        test_module_kwargs=dict(
            use_local_func=use_local_func, nnums=nnums, nprocs=nprocs,
        ),
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
    run_func: Callable[..., CompletedProcess],
    request: pytest.FixtureRequest,
    pool_test_module: ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    runner: str | list[str],
    outfile: str | None,
    profile: bool,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str,
) -> None:
    """
    Check that `kernprof` can RUN a test module in various contexts
    (`kernprof [...] <path>`, `kernprof [...] -m <module>`, and
    `kernprof [...] -c "code"`).

    Notes:
        - See issue #422 for the original motivation.

        - This test does not test the actual profiling, just the
          execution of the code and presence of profiling data
          thereafter.

        - Because this is mostly a sanity check, we only run one of the
          test modules.
    """
    run_func(
        request, pool_test_module, tmp_path_factory, runner, outfile, profile,
    )


_fuzz_prof_mp_run_func = Params.new(('run_func', 'label1'),
                                    [(run_module, 'module'),
                                     (run_script, 'script'),
                                     (run_literal_code, 'literal-code')],
                                    defaults=(run_script, 'script'))
_fuzz_prof_mp_markers = (
    (_fuzz_prof_mp_run_func
     + Params.new(('prof_child_procs', 'label2'),
                  [(True, 'with-child-prof'), (False, 'no-child-prof')])
     + _get_mp_start_method_fuzzer(None))
    # Test all `multiproc` start methods with both locally- and
    # externally-defined profiling targets
    * (Params.new(('preimports', 'label3'), [(False, 'no-preimports')])
       + Params.new(('use_local_func', 'label4'),
                    [(True, 'local'), (False, 'external')],
                    defaults=(False, 'external')))
    # Test all of the above with both test modules
    * Params.new('test_module', ['pool_test_module', 'process_test_module'])
    # The 'with-preimports' case is already tested rather thoroughly in
    # `test_apply_mp_patches()`, so exclude these from the above "main"
    # param matrix and just test the different `kernprof` modes via the
    # `run_func()`s
    + (_fuzz_prof_mp_run_func
       + Params.new(('preimports', 'label3'), [(True, 'with-preimports')]))
    # Just throw in a case where we actually run `kernprof` in a
    # subprocess, otherwise it is more convenient to do so in-process
    + Params.new(('subproc', 'label5'),
                 [(True, 'subproc'), (False, 'in-proc')],
                 defaults=(False, 'in-proc'))
).sorted().split_on_params('fail')


def _test_profiling_multiproc_script(
    run_func: Callable[..., CompletedProcess],
    request: pytest.FixtureRequest,
    test_module: ModuleFixture,
    ext_module: ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    prof_child_procs: bool,
    preimports: bool,
    use_local_func: bool,
    fail: bool,
    start_method: StartMethod | None,
    nnums: int,
    nprocs: int,
    **kwargs
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
        # `Pool.starmap()` (or `gather_results()`)
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
    kwargs.setdefault('timeout', DEFAULT_TIMEOUT)
    if prof_child_procs and DEBUG:
        kwargs.setdefault('debug_log', 'debug.log')
    run_func(
        request, test_module, tmp_path_factory,
        runner=runner,
        outfile='out.lprof',
        profile=True,
        fail=fail,
        start_method=start_method,
        nhits=nhits,
        test_module_kwargs=dict(
            use_local_func=use_local_func,
            nnums=nnums,
            nprocs=nprocs,
        ),
        **kwargs,
    )


@(_fuzz_prof_mp_markers[False])
@pytest.mark.parametrize(
    # XXX: should we explicitly test the single-proc case? We already
    # have quite a lot of subtests tho...
    ('nnums', 'nprocs'), [(2000, 3)],
)
def test_profiling_multiproc_script_success(
    run_func: Callable[..., CompletedProcess],
    request: pytest.FixtureRequest,
    test_module: str,
    ext_module: ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    prof_child_procs: bool,
    preimports: bool,
    use_local_func: bool,
    start_method: StartMethod | None,
    nnums: int,
    nprocs: int,
    subproc: bool,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str, label3: str, label4: str, label5: str,
) -> None:
    """
    Check that `kernprof` can PROFILE the test modules in various
    contexts when the parallel workload runs without errors, optionally
    extending profiling into child processes.

    Note:
        This test function is heavily parametrized. Here is why that is
        necessary:

        - ``run_func`` tests the different :cmd:`kernprof` modes (see
          :py:func:`~.test_running_multiproc_script`).

        - ``test_module`` chooses what kind of parallelism the test
          module should use (:py:class:`multiprocessing.pool.Pool` vs
          :py:class:`multiprocessing.process.BaseProcess`).

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
    module_fixture = request.getfixturevalue(test_module)
    assert isinstance(module_fixture, ModuleFixture)
    _test_profiling_multiproc_script(
        run_func=run_func,
        request=request,
        test_module=module_fixture,
        ext_module=ext_module,
        tmp_path_factory=tmp_path_factory,
        prof_child_procs=prof_child_procs,
        preimports=preimports,
        use_local_func=use_local_func,
        fail=False,
        start_method=start_method,
        nnums=nnums,
        nprocs=nprocs,
        subproc=subproc,
    )


@(_fuzz_prof_mp_markers[True])
@pytest.mark.parametrize(('nnums', 'nprocs'), [(2000, 3)])
def test_profiling_multiproc_script_failure(
    run_func: Callable[..., CompletedProcess],
    request: pytest.FixtureRequest,
    test_module: str,
    ext_module: ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    prof_child_procs: bool,
    preimports: bool,
    use_local_func: bool,
    start_method: StartMethod | None,
    nnums: int,
    nprocs: int,
    subproc: bool,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str, label3: str, label4: str, label5: str,
) -> None:
    """
    Check that `kernprof` can PROFILE the test modules in various
    contexts when the parallel workload errors out, optionally
    extending profiling into child processes.

    See also:
        :py:func:`test_profiling_multiproc_script_success`
    """
    module_fixture = request.getfixturevalue(test_module)
    assert isinstance(module_fixture, ModuleFixture)
    _test_profiling_multiproc_script(
        run_func=run_func,
        request=request,
        test_module=module_fixture,
        ext_module=ext_module,
        tmp_path_factory=tmp_path_factory,
        prof_child_procs=prof_child_procs,
        preimports=preimports,
        use_local_func=use_local_func,
        fail=True,
        start_method=start_method,
        nnums=nnums,
        nprocs=nprocs,
        subproc=subproc,
    )


_fuzz_bare = (
    Params.new(('use_subprocess', 'label1'),
               [(True, 'subprocess.run'), (False, 'os.system')])
    * Params.new(('prof_child_procs', 'label2'),
                 [(True, 'with-child-prof'), (False, 'no-child-prof')])
    * Params.new('n', [200])
)


def _test_profiling_bare_python(
    tmp_path_factory: pytest.TempPathFactory,
    ext_module: ModuleFixture,
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
    write_debug = DEBUG and prof_child_procs
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
    proc = run_subproc(
        cmd, text=True, capture_output=True, timeout=DEFAULT_TIMEOUT,
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
            check_tagged_line_nhits(proc.stdout, tag, num)
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
    ext_module: ModuleFixture,
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
    ext_module: ModuleFixture,
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
