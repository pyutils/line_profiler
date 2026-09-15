from __future__ import annotations

import atexit
import os
import multiprocessing
from collections.abc import Callable, Sequence
from functools import partial
from multiprocessing.process import BaseProcess
from types import MappingProxyType as mappingproxy
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast
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
    from multiprocessing import resource_tracker  # noqa
except ImportError:
    _CAN_USE_RESOURCE_TRACKER = False
else:
    _CAN_USE_RESOURCE_TRACKER = True

from .._patching_infrastructure import SingleModulePatch
from ..cache import LineProfilingCache
from ..runpy_patches import create_runpy_wrapper
from ._pool_patch_helpers import (
    get_per_task_callback_patch, get_worker_finalization_patch,
)


__all__ = (
    'POOL_WORKER_PID_PATCH', 'PROCESS_SETUP_PATCH', 'RESOURCE_TRACKER_PATCH',
    'RebootForkserverPatch', 'RunpyPatch',
    'wrap_bootstrap',
)

T = TypeVar('T')
P = TypeVar('P', bound=BaseProcess)
PS = ParamSpec('PS')

# ------------------------------ Helpers -------------------------------


def setup_mp_child(  # nocover
    cache: LineProfilingCache, proc: BaseProcess,
) -> None:
    """
    Perform :py:mod:`multiprocessing`-specific setup in a child process
    curated by the package. Currently it does the following:

    - Unregister the :py:mod:`atexit` hook associated with ``cache`` to
      avoid possible clashes with the profiling-file writing managed by
      this module.
    """
    _manage_mp_child(cache, 'setup', [_unregister_atexit_hook], proc)


def teardown_mp_child(cache: LineProfilingCache) -> None:  # nocover
    """
    Perform :py:mod:`multiprocessing`-specific teardown in a child
    process curated by the package. Currently it does the following:

    - Disable the :py:attr:`.LineProfilingCache.profiler` so that the
      trace callbacks are not called during interpreter teardown (e.g.
      when :py:mod:`atexit` hooks are called), which can result in noise
      (because facilities used by
      :py:mod:`line_profiler._line_profiler` are being pulled out from
      underneath it)
    """
    _manage_mp_child(cache, 'teardown', [_disable_cache_profiler])


def _manage_mp_child(
    cache: LineProfilingCache,
    action: str,
    callbacks: Sequence[Callable[Concatenate[LineProfilingCache, PS], Any]],
    /,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> None:
    if cache.main_pid == os.getpid():  # Not in a child process
        return
    xc: Exception | None = None
    msg = (
        f'Performing {action.lower()} for `multiprocessing` child processes...'
    )
    cache._debug_output(msg)
    for setup in callbacks:
        try:
            setup(cache, *args, **kwargs)
        except Exception as e:
            xc = e
    if xc is None:
        state = 'succeeded'
    else:
        state = f'failed: {type(xc).__name__}'
        if str(xc):
            state = f'{state}: {xc}'
    msg = (
        f'{action.capitalize()} for `multiprocessing` child process {state}'
    )
    cache._debug_output(msg)
    if xc is not None:
        raise xc


def _disable_cache_profiler(cache: LineProfilingCache) -> None:
    prof = cache.profiler
    if prof is None:
        return
    if TYPE_CHECKING:
        assert hasattr(prof, 'enable_count')
        assert isinstance(prof.enable_count, int)
    for _ in range(prof.enable_count):
        prof.disable_by_count()


def _unregister_atexit_hook(  # nocover
    cache: LineProfilingCache, _,
) -> None:
    atexit.unregister(cache._atexit_hook)


# ----------- `multiprocessing.process.BaseProcess` patches ------------


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
    and teardowns specific to :py:mod:`multiprocessing`-managed
    processes.
    """
    try:
        setup_mp_child(cache, self)
        return vanilla_impl(self, *args, **kwargs)
    finally:
        teardown_mp_child(cache)


PROCESS_SETUP_PATCH = SingleModulePatch('multiprocessing.process', priority=1)
PROCESS_SETUP_PATCH.add_method('BaseProcess', '_bootstrap', wrap_bootstrap)

# ---------------------- PID bookkeeping patches -----------------------


def _get_worker_ntasks(cache: LineProfilingCache, worker: BaseProcess) -> int:
    """
    Check if the process has run any tasks; if not, report to the cache.

    Returns:
        Number of tasks run by ``worker``
    """
    pid: int | None = getattr(worker, 'pid', None)
    ntasks_finalized = _get_ntasks_finalized(cache)
    if pid is None:  # Dummy process
        return 0
    key = id(worker), pid
    try:
        return ntasks_finalized[key]
    except KeyError:
        pass
    ntasks = _get_ntasks(cache).pop(pid, 0)
    msg = 'Worker {0.name!r} (PID: {0.pid}) ran {1} task(s)'
    cache._debug_output(msg.format(worker, ntasks))
    if not ntasks:
        cache._warn_possible_lack_of_stats(pid)
    return ntasks_finalized.setdefault(key, ntasks)


def _increment_ntasks(cache: LineProfilingCache, pid: int) -> None:
    """
    Take and process the PID of the child process completing the task.
    """
    ntasks = _get_ntasks(cache)
    ntasks[pid] = ntasks.get(pid, 0) + 1


def _get_ntasks(cache: LineProfilingCache) -> dict[int, int]:
    key = 'mp_proc_ntasks'
    return cache._additional_data.setdefault(key, cast(dict[int, int], {}))


def _get_ntasks_finalized(
    cache: LineProfilingCache,
) -> dict[tuple[int, int], int]:
    key = 'mp_proc_ntasks_finalized'
    return cache._additional_data.setdefault(
        key, cast(dict[tuple[int, int], int], {})
    )


def _get_pid(_) -> int:
    return os.getpid()


POOL_WORKER_PID_PATCH = get_per_task_callback_patch(
    _get_pid, _increment_ntasks, '__line_profiler_pool_worker_pid__',
)
get_worker_finalization_patch(_get_worker_ntasks, POOL_WORKER_PID_PATCH)

# --------------------------- Misc. patches ----------------------------


@LineProfilingCache._method_wrapper
def wrap_main(
    cache: LineProfilingCache, vanilla_impl: Callable[PS, T], /,
    *args: PS.args, **kwargs: PS.kwargs
) -> T:
    """
    Wrap around :py:func:`multiprocessing.resource_tracker.main` to
    tear the profiling stuff down.

    Note:
        The ``ResourceTracker`` server process is spawned when the first
        :py:mod:`multiprocessing` child process is created via the
        ``spawn`` or ``forkserver`` start methods. While this server
        process does not meaningfully contribute to the profiling result
        either way, since it can be created with profiling set up, its
        longevity means that:

        - :py:meth:`.LineProfilingCache.gather_stats` may catch empty
          .lprof files which it has occupied but not written to,
          resulting in a warning in the main process.

        - When the main process exits and takes with it the server
          process, said server process may emit errors because resources
          used by its :py:class:`.LineProfilingCache` instance (e.g.
          :py:attr:`.LineProfilingCache.cache_dir`) have already been
          torn down by the cache instance in the main process.
    """
    callbacks: list[Callable[[], Any]] = []

    callbacks.append(partial(atexit.unregister, cache._atexit_hook))
    reason = 'resource-tracker server process not to be profiled'
    callbacks.append(partial(cache.cleanup, reason=reason))
    if cache._stats_helper is not None:
        callbacks.append(partial(os.unlink, cache._stats_helper.outfile))

    for callback in callbacks:
        try:
            callback()
        except Exception:
            pass
    return vanilla_impl(*args, **kwargs)


RESOURCE_TRACKER_PATCH = SingleModulePatch('multiprocessing.resource_tracker')
if _CAN_USE_RESOURCE_TRACKER:
    RESOURCE_TRACKER_PATCH.add_method('', 'main', wrap_main)


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
        :py:meth:`multiprocessing.forkserver.ForkServer._stop()` which
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
        # Make sure this happens AFTER we've torn down all the machinery
        # (e.g. the .pth file, the environment variables)
        cache.add_cleanup_with_priority(cls.reboot, -1)

    @staticmethod
    def reboot() -> None:
        fs_obj = forkserver._forkserver
        stop = getattr(fs_obj, '_stop', None)
        # Appease the type-checker since `._stop()` is not public API
        if not callable(stop):  # nocover
            msg = f'ForkServer._stop() (= {stop!r}) not callable'
            raise AssertionError(msg)  # Shouldn't happen
        stop()


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
