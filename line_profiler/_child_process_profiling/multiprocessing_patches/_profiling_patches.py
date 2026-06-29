from __future__ import annotations

from collections.abc import Callable
from functools import partial
from multiprocessing.process import BaseProcess
from typing import TypeVar
from typing_extensions import Concatenate, ParamSpec

from ..cache import LineProfilingCache
from ._infrastructure import SingleModulePatch
from ._queue import Queue, PutWrapper


__all__ = (
    'POOL_PATCH', 'PROCESS_PATCH',
    'wrap_bootstrap', 'wrap_process', 'wrap_worker',
)

T = TypeVar('T')
P = TypeVar('P', bound=BaseProcess)
PS = ParamSpec('PS')

_POOL_WORKER_MARKER = '__line_profiler_multiprocessing_is_pool_worker__'

# ------------------------------ Helpers -------------------------------


def dump_stats_quick(
    cache: LineProfilingCache, *, reason: str | None = None,
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
    if cache.debug:
        stats_dumper.cleanup(force=True, reason=reason)
    else:
        stats_dumper()


def _mark_worker(worker: P) -> P:
    setattr(worker, _POOL_WORKER_MARKER, True)
    return worker


def _is_marked_worker(proc: BaseProcess) -> bool:
    return getattr(proc, _POOL_WORKER_MARKER, False)


# ---------------- `multiprocessing.pool.Pool` patches -----------------


@LineProfilingCache._method_wrapper  # nocover
def wrap_worker(
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

        - In an ideal world, we would have just written profiling output
          once as :py:func:`multiprocessing.pool.worker` returns. But:

          - Worker sometimes end up in "dirty" states and deadlock, and
            thus has to be terminated.

          - However, terminating a Python process bypasses the
            interpreter control flow, meaning that :py:mod:`atexit`
            hooks and ``try``-``finally`` blocks aren't executed.

          - On POSIX, this can be mitigated by setting signal handlers,
            but signal handling is infamously unreliable on
            :py:mod:`multiprocessing` child processes (examples:
            `1`_, `2`_, `3`_), causing hangs that are hard to remedy.

          So this is about as good as we can do.

    .. _1: https://github.com/python/cpython/issues/73945
    .. _2: https://github.com/python/cpython/issues/82408
    .. _3: https://github.com/coveragepy/coveragepy/issues/1310
    """
    dump = partial(dump_stats_quick, cache, reason='processed task')
    outqueue = PutWrapper(outqueue, dump)
    return vanilla_impl(inqueue, outqueue, *args, **kwargs)


@LineProfilingCache._method_wrapper
def wrap_process(
    _, vanilla_impl: Callable[PS, P], *args: PS.args, **kwargs: PS.kwargs
) -> P:
    """
    Wrap around :py:meth:`multiprocessing.pool.Pool.Process` so that the
    worker processes created by the pool are marked and can be
    distinguished from processes otherwise managed.

    Notes:

        - :py:meth:`multiprocessing.pool.Pool.Process` is a static
          method.

        - Technically one can inspect the :py:attr:`.BaseProcess.name`
          of the process to see that it is a ``PoolWorker``, but since
          said attribute is writable it may be more robust to set up a
          separate marker.
    """
    return _mark_worker(vanilla_impl(*args, **kwargs))


POOL_PATCH = SingleModulePatch('pool')
POOL_PATCH.add_method('', 'worker', wrap_worker)
POOL_PATCH.add_method('Pool', 'Process', wrap_process, 'static')

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
    Wrap around :py:meth:`.BaseProcess._bootstrap` so that profiling
    stats are written at the end.

    Notes:

        - This is only invoked in child processes, and
          :py:mod:`coverage` seems to be having trouble with them in the
          current setup, probably due to issues with .pth file
          precendence causing :py:mod:`line_profiler` to be loaded
          before it. Hence the ``# nocover``.

        - Since process termination bypasses the Python interpreter (see
          notes in :py:func:`wrap_worker`), if a child process is
          terminated prematurely (e.g. via
          :py:meth:`.BaseProcess.terminate`), profiling data may be
          missing.

        - To prevent data corruption/loss, the end-of-function write to
          the temporary profiling-stat file only happens for
          non-pool-managed :py:class:`BaseProcess` objects, because they
          are regularly :py:meth:`.BaseProcess.terminate`-ed by their
          managing pool.
    """
    try:
        return vanilla_impl(self, *args, **kwargs)
    finally:
        reason = 'exiting `multiprocessing.process.BaseProcess._bootstrap`'
        if not _is_marked_worker(self):
            dump_stats_quick(cache, reason=reason)


PROCESS_PATCH = SingleModulePatch('process')
PROCESS_PATCH.add_method('BaseProcess', '_bootstrap', wrap_bootstrap)
