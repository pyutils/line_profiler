"""
Hooks to set up shop in a child Python process and extend profiling
to therein.

Note:
    - The current implementation writes temporary .pth files to the
      site-packages directory, which are executed for all Python
      processes referring to the same :path:`lib/`. However, only
      processes originating from a parent which set the requisite
      environment variables will execute to the profiling code.
    - Said .pth file always import this module; hence, this file is kept
      intentionally lean to reduce overhead:
      - Imports in this file are deferred to being as late as possible.
      - Type annotations are replaced with type comments.
      - Non-essential functionalities are split into small separate
        submodules (e.g. :py:mod:`~.cache`).
    - Inspired by similar code in :py:mod:`coverage.control` and
      :py:mod:`pytest_autoprofile.startup_hook`.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cache import LineProfilingCache  # noqa: F401
    from pathlib import Path  # noqa: F401


__all__ = (
    'write_pth_hook', 'load_pth_hook', '_setup_in_child_process'
)

INHERITED_PID_ENV_VARNAME = (
    'LINE_PROFILER_PROFILE_CHILD_PROCESSES_CACHE_PID'
)


def write_pth_hook(cache):  # type: (LineProfilingCache) -> Path
    """
    Write a .pth file which allows for setting up profiling in child
    Python processes.

    Args:
        cache (:py:class:`~.LineProfilingCache`):
            Cache object

    Returns:
        fpath (Path):
            Path to the written .pth file

    Note:
        - To be called in the main process.
        - The ``cache`` is responsible for deleting the written .pth
          file via the registered cleanup callback.
        - For convenience, we also wrap :py:func:`os.fork` when this
          function is called.
    """
    import os
    from pathlib import Path  # noqa: F811
    from sysconfig import get_path
    from tempfile import mkstemp

    if not os.path.exists(cache.filename):
        cache.dump()
        assert os.path.exists(cache.filename)

    handle, fname = mkstemp(
        prefix='_line_profiler_profiling_hook_',
        suffix='.pth',
        dir=get_path('purelib'),
    )
    fpath = Path(fname)
    try:
        pth_content = 'import {0}; {0}.load_pth_hook({1})'.format(
            (lambda: None).__module__, cache.main_pid,
        )
        fpath.write_text(pth_content)
        cache.add_cleanup(fpath.unlink, missing_ok=True)
    except Exception:
        os.remove(fpath)
        raise
    finally:  # Not closing the handle causes issues on Windows
        os.close(handle)

    _wrap_os_fork(cache)

    return fpath


def load_pth_hook(ppid):  # type: (int) -> None
    """
    Function imported and called by the written .pth file; to reduce
    overhead, we immediately return if ``ppid`` doesn't match
    :env:`LINE_PROFILER_PROFILE_CHILD_PROCESSES_CACHE_PID`.
    """
    from os import environ

    try:
        env_ppid = int(environ[INHERITED_PID_ENV_VARNAME])
    except (KeyError, ValueError):
        return
    if env_ppid != ppid:
        return

    # If we're here, we're most probably in a descendent process of a
    # profiled Python process, so we can be more liberal with the
    # imports without worrying about overhead
    import warnings
    from .._diagnostics import DEBUG, log
    from .cache import LineProfilingCache  # noqa: F811

    # Note: .pth files may be double-loaded in a virtual environment
    # (see https://stackoverflow.com/questions/58807569), so work around
    # that;
    # also see similar check in `coverage.control.process_startup()`
    if getattr(load_pth_hook, 'called', False):
        return
    try:
        cache = LineProfilingCache.load()
        cache._debug_output(f'cache {id(cache):#x} setting up (pth)...')
        has_set_up = _setup_in_child_process(cache, True)
        cache._debug_output('cache {:#x} setup {}'.format(
            id(cache), 'done' if has_set_up else 'aborted',
        ))
        # _setup_in_child_process(LineProfilingCache.load())
    except Exception as e:
        if DEBUG:
            msg = f'{type(e)}: {e}'
            warnings.warn(msg)
            log.warning(msg)
    finally:
        load_pth_hook.called = True  # type: ignore[attr-defined]


def _wrap_os_fork(cache):  # type: (LineProfilingCache) -> None
    """
    Create a wrapper around :py:func:`os.fork` which handles profiling.

    Args:
        cache (:py:class:`~.LineProfilingCache`):
            Cache object

    Side effects:
        - :py:func:`os.fork` (if available) replaced with the wrapper
        - Cleanup callback registered at ``cache`` undoing that
    """
    import os
    from functools import wraps

    try:
        fork = os.fork
    except AttributeError:  # Can't fork on this platform
        return

    @wraps(fork)
    def wrapper():
        result = fork()
        if result:
            return result
        # If we're here, we are in the fork
        forked = cache.copy()
        if forked._replace_loaded_instance():
            forked._debug_output(
                f'cache {id(forked):#x} in fork '
                'superseded cached `.load()`-ed instance'
            )
        forked._debug_output(f'cache {id(forked):#x} setting up (fork)...')
        has_set_up = _setup_in_child_process(forked)
        forked._debug_output('cache {:#x} setup {}'.format(
            id(forked), 'done' if has_set_up else 'aborted',
        ))
        return result

    os.fork = wrapper
    cache.add_cleanup(setattr, os, 'fork', fork)


def _setup_in_child_process(cache, wrap_os_fork=False):
    # type: (LineProfilingCache, bool) -> bool
    """
    Set up shop in a forked/spawned child process so that
    (line-)profiling can extend therein.

    Args:
        cache (:py:class:`~.LineProfilingCache`):
            Cache object
        wrap_os_fork (bool):
            Whether to wrap :py:func:`os.fork` which handles profiling;
            already-forked child processes should set this to false

    Returns:
        has_set_up (bool):
            False is ``cache`` has already been set up prior to calling
            this function, true otherwise
    """
    if cache.profiler is not None:  # Already set up
        return False

    import os
    from atexit import register
    from tempfile import mkstemp
    from ..autoprofile.autoprofile import (
        # Note: we need this to equip the profiler with the
        # `.add_imported_function_or_module()` pseudo-method
        # (see `kernprof.py::_write_preimports()`), which is required
        # for the preimports to work
        _extend_line_profiler_for_profiling_imports as upgrade_profiler,
    )
    from ..curated_profiling import CuratedProfilerContext
    from ..line_profiler import LineProfiler
    from .import_machinery import RewritingFinder

    # Create a profiler instance and manage it with
    # `CuratedProfilerContext`
    cache.profiler = prof = LineProfiler()
    upgrade_profiler(prof)
    ctx = CuratedProfilerContext(prof, insert_builtin=cache.insert_builtin)
    ctx.install()
    cache.add_cleanup(ctx.uninstall)

    # Do the preimports at `cache.preimports_module` where appropriate
    if cache.preimports_module:
        cache._debug_output(f'cache {id(cache):#x} loading preimports...')
        with open(cache.preimports_module, mode='rb') as fobj:
            code = compile(fobj.read(), cache.preimports_module, 'exec')
            exec(code, {})  # Use a fresh, empty namespace

    # Set up the importer for rewriting `__main__`
    finder = RewritingFinder(prof, cache)
    finder.install()
    cache.add_cleanup(finder.uninstall)

    # Occupy a tempfile slot in `cache.cache_dir` and set the profiler
    # up to write thereto when the process terminates
    handle, prof_outfile = mkstemp(
        prefix='child-prof-output-{}-{}-{:#x}-'
        .format(cache.main_pid, os.getpid(), id(prof)),
        suffix='.lprof',
        dir=cache.cache_dir,
    )
    try:  # Whatever else we do, write the profiling stats first
        cache._add_cleanup(prof.dump_stats, -1, prof_outfile)
    finally:
        os.close(handle)

    # Set up `os.fork()` wrapping if needed (i.e. in a spawned process)
    if wrap_os_fork:
        _wrap_os_fork(cache)

    # Set `cache.cleanup()` as an atexit hook to handle everything when
    # the child process is about to terminate
    register(cache.cleanup)

    return True
