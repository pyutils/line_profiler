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
    from pathlib import Path  # noqa: F401
    from .cache import LineProfilingCache  # noqa: F401


__all__ = ('write_pth_hook', 'load_pth_hook')

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
        cache._setup_in_child_process(True, 'pth')
    except Exception as e:
        if DEBUG:
            msg = f'{type(e)}: {e}'
            warnings.warn(msg)
            log.warning(msg)
    finally:
        load_pth_hook.called = True  # type: ignore
