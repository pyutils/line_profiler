from __future__ import annotations

import atexit
import os
import multiprocessing
from collections.abc import Callable
from functools import partial
from multiprocessing.process import BaseProcess
from pathlib import Path
from types import MappingProxyType as mappingproxy, MethodType
from typing import Any, ClassVar, TypeVar, cast
from typing_extensions import Concatenate, ParamSpec

try:
    from multiprocessing import spawn
except ImportError:
    _CAN_USE_SPAWN = False
else:
    _CAN_USE_SPAWN = True
try:
    from multiprocessing import forkserver
except ImportError:
    _CAN_USE_FORKSERVER = False
else:
    _CAN_USE_FORKSERVER = (
        'forkserver' in multiprocessing.get_all_start_methods()
    )
try:
    from multiprocessing import resource_tracker
except ImportError:
    _CAN_USE_RESOURCE_TRACKER = False
else:
    _CAN_USE_RESOURCE_TRACKER = True

from ..cache import LineProfilingCache
from ..runpy_patches import create_runpy_wrapper
from ._infrastructure import SingleModulePatch
from .mp_config import MPConfig
from .poller import Poller, OnTimeout


__all__ = (
    'PROCESS_TERMINATION_PATCH',
    'RebootForkserverPatch', 'ResourceTrackerPatch', 'RunpyPatch',
    'wrap_terminate', 'wrap_start', 'wrap_bootstrap',
)

_LOCK_FILE_LOC = '__line_profiler_multiprocessing_process_lock_file__'

T = TypeVar('T')
PS = ParamSpec('PS')

# ------------------------------ Helpers -------------------------------


def setup_mp_child(  # nocover
    cache: LineProfilingCache, proc: BaseProcess,
) -> None:
    """
    Perform :py:mod:`multiprocessing`-specific setup in a child process
    curated by the module. Currently it does the following:

    - Unregister the :py:mod:`atexit` hook associated with ``cache`` to
      avoid possible clashes with the profiling-file writing managed by
      this module.

    - Remove the per-child-process lock file which prevents the parent
      from :py:meth:`.BaseProcess.terminate`-ing it before it can be
      properly set up and causing a hang.
    """
    if cache.main_pid == os.getpid():  # Not in a child process
        return
    xc: Exception | None = None
    msg = 'Performing setup for `multiprocessing` child processes...'
    cache._debug_output(msg)
    setup: Callable[[LineProfilingCache, BaseProcess], Any]
    for setup in [_unregister_atexit_hook, _remove_lock_file]:
        try:
            setup(cache, proc)
        except Exception as e:
            xc = e
    if xc is None:
        msg = 'Setup for `multiprocessing` child process succeeded'
        cache._debug_output(msg)
    else:
        xc_str = type(xc).__name__
        if str(xc):
            xc_str = f'{xc_str}: {xc}'
        cache._debug_output(f'Setup failed: {xc_str}')
        raise xc


def _unregister_atexit_hook(  # nocover
    cache: LineProfilingCache, _,
) -> None:
    atexit.unregister(cache._atexit_hook)


def _remove_lock_file(  # nocover
    cache: LineProfilingCache, proc: BaseProcess,
) -> None:
    lock_file = _get_lock_file(proc)
    if lock_file is None:
        return
    lock_file.unlink(missing_ok=True)
    cache._debug_output(f'Removed lock file {lock_file.name!r}')


def _get_lock_file(proc: BaseProcess) -> Path | None:
    return getattr(proc, _LOCK_FILE_LOC, None)


def _set_lock_file(proc: BaseProcess, lock_file: Path) -> None:
    setattr(proc, _LOCK_FILE_LOC, lock_file)


# ----------- `multiprocessing.process.BaseProcess` patches ------------


@LineProfilingCache._method_wrapper
def wrap_terminate(
    cache: LineProfilingCache,
    vanilla_impl: Callable[[BaseProcess], None],
    self: BaseProcess,
) -> None:
    """
    Wrap around :py:meth:`.BaseProcess.terminate` to make sure that we
    don't attempt to kill the child (OS-level) process before it has
    been set up, by polling for when the child process has completed
    setup and deleted its lock file.

    See also:
        :py:func:`line_profiler._child_process_profiling.\
multiprocessing_patches._profiling_patches.wrap_terminate`
    """
    lock_file = _get_lock_file(self)
    if lock_file is None:
        cache._debug_output(f'no lock file associated with {self!r}')
        vanilla_impl(self)
        return
    try:
        with _get_terminate_poller(cache, self, lock_file):
            pass
    except Poller.Timeout as e:  # Also handles `~.TimeoutWarning`
        cache._debug_output(f'{type(e).__qualname__}: {e}')
        raise
    finally:  # Always call `Process.terminate()` to avoid orphans
        vanilla_impl(self)


@LineProfilingCache._method_wrapper
def wrap_start(
    cache: LineProfilingCache,
    vanilla_impl: Callable[[BaseProcess], None],
    self: BaseProcess,
) -> None:
    """
    Wrap around :py:meth:`.BaseProcess.start` to make sure that we
    don't attempt to kill the child (OS-level) process before it has
    been set up, by setting up a lock file which the child process
    should delete upon completing setup.
    """
    prefix = f'process-termination-lock-{os.getpid()}-{id(self):#x}-'
    # This assigns the tempfile to the instance dict, which should be
    # pickled along with the rest of the instance and sent to the child
    # process
    _set_lock_file(self, cache.make_tempfile(prefix=prefix, suffix='.lock'))
    vanilla_impl(self)


@LineProfilingCache._method_wrapper  # nocover
def wrap_bootstrap(
    cache: LineProfilingCache,
    vanilla_impl: Callable[Concatenate[BaseProcess, PS], T],
    self: BaseProcess,
    /,
    *args: PS.args, **kwargs: PS.kwargs
) -> T:
    """
    Wrap around :py:meth:`.BaseProcess._bootstrap` to perform setups
    and signal to the parent process thereafter.

    Notes:
        This is only invoked in child processes, and
        :py:mod:`coverage` seems to be having trouble with them in the
        current setup, probably due to issues with .pth file
        precendence causing :py:mod:`line_profiler` to be loaded
        before it. Hence the ``# nocover``.
    """
    setup_mp_child(cache, self)
    return vanilla_impl(self, *args, **kwargs)


def _get_terminate_poller(
    cache: LineProfilingCache, process: BaseProcess, lock_file: Path,
) -> Poller:
    config = MPConfig.from_cache(cache)
    cd, timeout, on_timeout = config.polling
    if on_timeout not in ('ignore', 'warn', 'error'):
        on_timeout = config.get_defaults().polling.on_timeout
    return (
        Poller.poll_until(_lock_file_removed, cache, process, lock_file)
        .with_cooldown(cd)
        .with_timeout(timeout, cast(OnTimeout, on_timeout))
    )


def _lock_file_removed(
    cache: LineProfilingCache, proc: BaseProcess, path: Path,
) -> bool:
    exists = path.exists()
    if exists:
        msg = (
            f'Waiting for process {proc.ident} to set up and '
            f'delete the lock file {path.name!r}...'
        )
    else:
        msg = f'Process {proc.ident} has been set up'
    cache._debug_output(f'  {type(proc).__name__} @ {id(proc):#x}: {msg}')
    return not exists


PROCESS_TERMINATION_PATCH = SingleModulePatch(
    'process', priority=1,
).add_target(
    'BaseProcess',
    {
        'terminate': wrap_terminate,
        'start': wrap_start,
        '_bootstrap': wrap_bootstrap,
    },
)

# --------------------------- Misc. patches ----------------------------


class RebootForkserverPatch:
    """
    Reboot the process backing the global
    :py:class:`multiprocessing.forkserver.ForkServer` instance:

    - When the patch is applied, so as to ensure that child processes
      forked therefrom actually receives the active patches; and

    - When the session cache is cleaned up, so that child processes
      forked therefrom is no longer polluted by the patches.

    Note:
        This uses
        :py:method:`multiprocessing.forkserver.ForkServer._stop()` which
        is private API, but it's the same hack used in Python's own test
        suite -- see the comment to said method.
    """
    summary: ClassVar[mappingproxy[str, frozenset[str]]] = mappingproxy({})
    priority: ClassVar[float | None] = None

    @classmethod
    def apply(cls, cache: LineProfilingCache, **_) -> None:
        if not _CAN_USE_FORKSERVER:
            return
        cls.reboot()
        cache.add_cleanup(cls.reboot)

    @staticmethod
    def reboot() -> None:
        # Appease the type-checker since `._stop()` is not public API
        stop = getattr(forkserver._forkserver, '_stop', None)
        assert callable(stop)
        stop()


class ResourceTrackerPatch:
    """
    Patch :py:mod:`multiprocessing.resource_tracker` so that
    :py:func:`multiprocessing.resource_tracker.ensure_running` and the
    eponymous method of
    :py:class:`multiprocessing.resource_tracker.ResourceTracker` report
    the resource-tracker server PIDs to the session cache.

    Note:
        The ``ResourceTracker`` server process is spawned when the first
        :py:mod:`multiprocessing` child process is created via the
        ``spawn`` or ``forkserver`` start methods. While this server
        process does not meaningfully contribute to the profiling result
        either way, since it can be created with profiling set up, its
        longevity means that :py:meth:`.LineProfilingCache.gather_stats`
        often catches empty .lprof files which it has occupied but not
        written to.

        To reduce noise while keeping the empty-file warning for other
        output files, we report the PIDs used by the server to the
        session cache so that they can be ignored if necessary.
    """
    if _CAN_USE_RESOURCE_TRACKER:
        summary: ClassVar[mappingproxy[str, frozenset[str]]] = mappingproxy({
            'multiprocessing.resource_tracker':
            frozenset({'ensure_running'}),
            'multiprocessing.resource_tracker.ResourceTracker':
            frozenset({'ensure_running'}),
        })
    else:
        summary = mappingproxy({})
    priority: ClassVar[float | None] = None

    @staticmethod
    @LineProfilingCache._method_wrapper
    def wrap_ensure_running(
        cache: LineProfilingCache,
        vanilla_impl: Callable[['resource_tracker.ResourceTracker'], None],
        self: 'resource_tracker.ResourceTracker',
    ) -> None:
        """
        Wrap around :py:meth:`multiprocessing.resource_tracker\
.ResourceTracker.ensure_running`
        so that the session cache can keep track of the PIDs used by the
        resource-tracer server.
        """
        maybe_pids: set[int | None] = {getattr(self, '_pid', None)}
        try:
            vanilla_impl(self)
        finally:
            maybe_pids.add(getattr(self, '_pid', None))
            pids = cast(set[int], maybe_pids - {None})
            if pids:
                cache._warn_possible_lack_of_stats(pids)

    @classmethod
    def apply(
        cls, cache: LineProfilingCache, *, cleanup: bool = True, **_,
    ) -> list[str]:
        if _CAN_USE_RESOURCE_TRACKER:
            patch = partial(cache.patch, cleanup=cleanup)
            # Patch the method on the class
            method = resource_tracker.ResourceTracker.ensure_running
            method = cls.wrap_ensure_running(method)
            patch(resource_tracker.ResourceTracker, 'ensure_running', method)
            # Patch the preexisting bound method on the module
            instance = resource_tracker._resource_tracker
            bound_method = MethodType(method, instance)
            patch(resource_tracker, 'ensure_running', bound_method)
        return list(cls.summary)


class RunpyPatch:
    """
    Patch the copy of :py:mod:`runpy` in the
    :py:mod:`multiprocessing.spawn` namespace so that subprocesses can
    perform rewrite-based profiling as with
    :py:func:`line_profiler.autoprofile.autoprofile.run`.

    See also:
        :py:mod:`line_profiler._child_process_profiling.runpy_patches`
    """
    summary: ClassVar[mappingproxy[str, frozenset[str]]]
    if _CAN_USE_SPAWN and hasattr(spawn, 'runpy'):
        summary = mappingproxy({'multiprocessing.spawn': frozenset({'runpy'})})
    else:
        summary = mappingproxy({})
    priority: ClassVar[float | None] = None

    @classmethod
    def apply(
        cls, cache: LineProfilingCache, *, cleanup: bool = True, **_,
    ) -> list[str]:
        if cls.summary:
            patch = partial(cache.patch, cleanup=cleanup)
            patch(spawn, 'runpy', create_runpy_wrapper(cache))
        return list(cls.summary)
