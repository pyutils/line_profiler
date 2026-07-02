"""
Additional hooks installed by :py:mod:`line_profiler`.

Notes:
    - This file and its content should be considered an implmentation
      detail of :py:mod:`line_profiler`; currently we just use this to
      set up shop in a child Python process, and extend profiling to
      therein.

    - This current implementation writes temporary .pth files to the
      site-packages directory, which are executed for all Python
      processes referring to the same :path:`lib/`. However, only
      processes originating from a parent which set the requisite
      environment variables will execute to the profiling code.

    - Said .pth file always import this module; hence, this file is kept
      intentionally lean and separate from the main
      :py:mod:`line_profiler` package to reduce overhead; e.g.
      imports in this file are deferred to being as late as possible.

    - Inspired by similar code in :py:mod:`coverage.control` and
      :py:mod:`pytest_autoprofile.startup_hook`.
"""
import os


__all__ = ('load_pth_hook',)

INHERITED_PID_ENV_VARNAME = (
    'LINE_PROFILER_PROFILE_CHILD_PROCESSES_CACHE_PID'
)


def load_pth_hook(ppid: int) -> None:
    """
    Function imported and called by the written .pth file; to reduce
    overhead, we immediately return if ``ppid`` doesn't match
    :env:`LINE_PROFILER_PROFILE_CHILD_PROCESSES_CACHE_PID`.
    """
    try:
        env_ppid = int(os.environ[INHERITED_PID_ENV_VARNAME])
    except (KeyError, ValueError):
        return
    if env_ppid != ppid:
        return

    # If we're here, we're most probably in a descendent process of a
    # profiled Python process, so we can be more liberal with the
    # imports without worrying about overhead
    import warnings
    from line_profiler._diagnostics import DEBUG, log
    from line_profiler._child_process_profiling.cache import LineProfilingCache

    # Note: .pth files may be double-loaded in a virtual environment
    # (see https://stackoverflow.com/questions/58807569), so work around
    # that;
    # also see similar check in `coverage.control.process_startup()`
    if getattr(load_pth_hook, 'called', False):
        return
    try:
        cache = LineProfilingCache.load()
        cache._setup_in_child_process(True, 'pth')
    except Exception as e:  # nocover
        if DEBUG:
            msg = f'{type(e)}: {e}'
            # Write log befor issuing warning, in case the warning is
            # promoted to an exception
            log.warning(msg)
            warnings.warn(msg)
        load_pth_hook.called = True  # type: ignore
    else:
        cache.patch(load_pth_hook, 'called', True)
