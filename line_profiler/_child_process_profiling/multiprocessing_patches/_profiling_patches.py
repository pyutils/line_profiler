from __future__ import annotations

import os
import sys
from collections.abc import Callable
from functools import partial
from multiprocessing.process import BaseProcess
from typing import Any, TypeVar, cast
from typing_extensions import Concatenate, ParamSpec

from ..cache import LineProfilingCache
from ._infrastructure import SingleModulePatch
from ._queue import Queue, PutWrapper
from .mp_config import MPConfig
from .poller import Poller, OnTimeout


__all__ = (
    'POOL_PATCH', 'PROCESS_PATCH',
    'wrap_bootstrap', 'wrap_terminate', 'wrap_worker',
)

T = TypeVar('T')
PS = ParamSpec('PS')

_CAN_CATCH_SIGTERM = sys.platform != 'win32'

# ------------------------------ Helpers -------------------------------


def dump_stats_quick(
    cache: LineProfilingCache,
    *,
    reason: str | None = None,
    debug: bool = False,
) -> None:
    """
    Note:
        We don't really care about cleanup in the child process, so just
        dump the stats and bail to reduce the chance of end-of-process
        shenanigans causing a deadlock...
        but do use ``._stats_dumper.cleanup()`` instead of
        ``.__call__()`` so that we get debugging output (if ``debug`` is
        true)
    """
    stats_dumper = cache._stats_dumper
    if stats_dumper is None:
        return
    if debug:
        stats_dumper.cleanup(force=True, reason=reason)
    else:
        stats_dumper()


# ---------------- `multiprocessing.pool.Pool` patches -----------------


class _PIDQueueGetWrapper:  # nocover
    """
    Wrapper around the ``inqueue`` argument to
    :py:func:`multiprocessing.pool.worker` to intercept the sentinel
    value (:py:const:`None`) signifying the end of the queue and perform
    cleanup.
    """
    def __init__(
        self,
        queue: Queue,
        cache: LineProfilingCache,
    ) -> None:
        self._queue = queue
        self._cache = cache

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._queue, attr)

    def put(self, obj: Any) -> None:
        self._queue.put(obj)

    def get(self) -> Any:
        result = self._queue.get()
        cache = self._cache
        ntasks: dict[int, int]
        ntasks = cache._additional_data.setdefault('mp_queue_ntasks', {})
        queue_id = id(self)
        if result is None:
            n = ntasks.pop(queue_id, 0)
            cache._debug_output(
                '`multiprocessing.pool.worker`: '
                f'recieved {n} task(s) in total',
            )
            # Got sentinel value, process is about to exit
            reason = 'ran out of tasks in `multiprocessing.process.worker()`'
            if cache.main_pid != os.getpid():
                dump_stats_quick(cache, debug=True, reason=reason)
        else:
            ntasks[queue_id] = ntasks.get(queue_id, 0) + 1
        return result


@LineProfilingCache._method_wrapper  # nocover
def wrap_worker_write_on_exit(
    cache: LineProfilingCache,
    vanilla_impl: Callable[Concatenate[Queue, PS], None],
    inqueue: Queue,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> None:
    """
    Wrap around :py:func:`multiprocessing.pool.worker` so that child
    processes can write profiling output as soon as the pool runs out of
    tasks.

    Notes:
        - This is only called in child processes and thus we can't
          reliably measure coverage thereon; see also
          :py:func:`wrap_bootstrap`.

        - This only works reliably for POSIX because we can handle
          ``SIGTERM`` on child processes and ensure that they aren't
          prematurely terminated.
    """
    return vanilla_impl(_PIDQueueGetWrapper(inqueue, cache), *args, **kwargs)


@LineProfilingCache._method_wrapper  # nocover
def wrap_worker_write_per_task(
    cache: LineProfilingCache,
    vanilla_impl: Callable[Concatenate[Queue, Queue, PS], None],
    inqueue: Queue,
    outqueue: Queue,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> None:
    """
    Wrap around :py:func:`multiprocessing.pool.worker` so that child
    processes can write profiling output before pushing the result of
    each task back to the parent.

    Notes:
        - This is only called in child processes and thus we can't
          reliably measure coverage thereon; see also
          :py:func:`wrap_bootstrap`.

        - This is only used on Windows where we can't handle ``SIGTERM``
          on child processes, thus necessitating the write to happen
          before control flow is passed backed to the parent.
    """
    outqueue = PutWrapper(outqueue, partial(dump_stats_quick, cache))
    return vanilla_impl(inqueue, outqueue, *args, **kwargs)


if _CAN_CATCH_SIGTERM:
    wrap_worker: Callable[[Callable[..., None]], Callable[..., None]]
    wrap_worker = wrap_worker_write_on_exit
else:
    wrap_worker = wrap_worker_write_per_task
POOL_PATCH = SingleModulePatch('pool').add_method(
    '', 'worker', wrap_worker,
)

# ----------- `multiprocessing.process.BaseProcess` patches ------------


@LineProfilingCache._method_wrapper
def wrap_terminate(
    cache: LineProfilingCache,
    vanilla_impl: Callable[[BaseProcess], None],
    self: BaseProcess,
) -> None:
    """
    Wrap around :py:meth:`.BaseProcess.terminate` to make sure that we
    don't actually kill the child (OS-level) process before it has the
    chance to properly clean up.

    Note:
        We're technically polling in a loop, but it isn't actually
        *that* bad: typically ``.terminate()`` is only called when we're
        on the bad path (e.g. the parallel workload errored out), and
        after the performance-critical part of the code (said workload).
    """
    try:
        with _get_terminate_poller(cache, self):
            pass
    except Poller.Timeout as e:  # Also handles `~.TimeoutWarning`
        cache._debug_output(f'{type(e).__qualname__}: {e}')
        raise
    finally:  # Always call `Process.terminate()` to avoid orphans
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
    Wrap around :py:meth:`.BaseProcess._bootstrap` so that profiling
    stats are written at the end.

    Notes:

        - This is only invoked in child processes, and
          :py:mod:`coverage` seems to be having trouble with them in the
          current setup, probably due to issues with .pth file
          precendence causing :py:mod:`line_profiler` to be loaded
          before it. Hence the ``# nocover``.

        - ``SIGTERM`` handling is not consistent on Windows, so we made
          :py:meth:`.LineProfilingCache._add_signal_handler` a no-op
          there. Hence :py:func:`wrap_terminate` remains necessary for
          mitigating unclean exits.
    """
    try:
        return vanilla_impl(self, *args, **kwargs)
    finally:
        reason = 'exiting `multiprocessing.process.BaseProcess._bootstrap`'
        dump_stats_quick(cache, debug=True, reason=reason)


def _get_terminate_poller(
    cache: LineProfilingCache, process: BaseProcess,
) -> Poller:
    config = MPConfig.from_cache(cache)
    cd, timeout, on_timeout = config.polling
    if on_timeout not in ('ignore', 'warn', 'error'):
        on_timeout = config.get_defaults().polling.on_timeout
    # `_process_has_returned()` takes a `timeout` which it passes to
    # `popen.wait()`; said timeout is essentially a limit as to how
    # often the function is called, hence our cooldown
    poller = Poller.poll_until(_process_has_returned, process, cache, cd)
    return poller.with_timeout(timeout, cast(OnTimeout, on_timeout))


def _process_has_returned(
    proc: BaseProcess, cache: LineProfilingCache, timeout: float,
) -> bool:
    popen = getattr(proc, '_popen', None)
    if popen is None:
        msg, result = 'No associated process', True
    else:
        result = popen.wait(timeout) is not None
        if result:
            msg = f'Process {popen.pid} has returned'
        else:
            msg = f'Waiting for process {popen.pid} to return...'
    cache._debug_output(f'  {type(proc).__name__} @ {id(proc):#x}: {msg}')
    return result


PROCESS_PATCH = SingleModulePatch('process').add_method(
    'BaseProcess', '_bootstrap', wrap_bootstrap,
)
# We only need to patch `Process.terminate()` if we can't do SIGTERM
# handling, i.e. on Windows
if not _CAN_CATCH_SIGTERM:
    PROCESS_PATCH.add_method('BaseProcess', 'terminate', wrap_terminate)
