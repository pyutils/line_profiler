from __future__ import annotations

from collections.abc import Callable
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import TypeVar, cast
from typing_extensions import Concatenate, ParamSpec

from ...line_profiler import LineStats
from .._patching_infrastructure import SingleModulePatch
from ..cache import LineProfilingCache
from ._pool_patch_helpers import (
    get_per_task_callback_patch, get_worker_finalization_patch,
)


__all__ = (
    'POOL_PATCH', 'PROCESS_PATCH',
    'wrap_bootstrap', 'wrap_process',
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
        but do use ``._stats_helper.cleanup()`` instead of
        ``.__call__()`` so that we get debugging output (if ``debug`` is
        true)
    """
    stats_helper = cache._stats_helper
    if stats_helper is None:
        return
    if cache.debug:
        stats_helper.cleanup(force=True, reason=reason)
    else:
        stats_helper()


def _mark_worker(worker: P) -> P:
    setattr(worker, _POOL_WORKER_MARKER, True)
    return worker


def _is_marked_worker(proc: BaseProcess) -> bool:
    return getattr(proc, _POOL_WORKER_MARKER, False)


# ---------------- `multiprocessing.pool.Pool` patches -----------------


@LineProfilingCache._method_wrapper
def wrap_process(
    _, vanilla_impl: Callable[PS, P], *args: PS.args, **kwargs: PS.kwargs
) -> P:
    """
    Wrap around :py:meth:`.Pool.Process` so that the worker processes
    created by the pool are marked and can be distinguished from
    processes otherwise managed.

    Notes:

        - :py:meth:`.Pool.Process` is a static method.

        - Technically one can inspect the :py:attr:`.BaseProcess.name`
          of the process to see that it is a ``PoolWorker``, but since
          said attribute is writable it may be more robust to set up a
          separate marker.
    """
    return _mark_worker(vanilla_impl(*args, **kwargs))


def _report_stats_and_dest(
    cache: LineProfilingCache,
) -> tuple[LineStats, Path] | None:  # nocover
    """
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

        - Instead of dumping the stats to disk every task, it should be
          less overhead for us to just send them back to the parent via
          the preexisting connection.

        - Unless the code paths varied significantly between tasks, the
          physical size of the stats should not have changed too much –
          running the same code more only increment the timing entries,
          but do not generate more thereof. Thus, calculating the
          delta-stats between tasks and only sending those would have
          been a waste of time here; on top of that, the parent would
          also have to perform additional processing to accumulate the
          deltas. So we just send the entire stats object.

    .. _1: https://github.com/python/cpython/issues/73945
    .. _2: https://github.com/python/cpython/issues/82408
    .. _3: https://github.com/coveragepy/coveragepy/issues/1310
    """
    stats_helper = cache._stats_helper
    if stats_helper is None:
        return None
    return stats_helper.get(), Path(stats_helper.outfile)


def _record_stats(
    cache: LineProfilingCache,
    stats_and_dest: tuple[LineStats, Path] | None,
) -> None:
    """
    Record the stats gathered from workers so that they can be dealt
    with when the process pool is terminated.

    See also:
        :py:func:`_report_stats_and_dest`
    """
    if stats_and_dest is None:
        return  # No-op
    stats, dest = stats_and_dest
    _get_worker_stats(cache)[dest] = stats


def _write_recorded_stats(
    cache: LineProfilingCache, worker: BaseProcess,
) -> None:
    """
    Write the gathered stats associated with ``worker``.
    """
    pid = getattr(worker, 'pid', None)
    if pid is None:
        return
    worker_stats = _get_worker_stats(cache)
    xc: Exception | None = None
    msg = '{0} (centralized `.dump_stats()`): {1.name!r} (PID: {1.pid}) -> {2}'
    for outfile in cache._get_profiling_outfiles(pid):
        stats = worker_stats.pop(outfile, None)
        if stats is None:
            continue
        try:
            stats.to_file(outfile)
        except Exception as e:
            xc = e
            state, outcome = 'Failed', cache._format_exception(e)
        else:
            state, outcome = 'Succeeded', repr(outfile.name)
        cache._debug_output(msg.format(state, worker, outcome))
    if xc is not None:
        raise xc


def _get_worker_stats(cache: LineProfilingCache) -> dict[Path, LineStats]:
    key = 'mp_pool_worker_stats'
    return cache._additional_data.setdefault(
        key, cast(dict[Path, LineStats], {}),
    )


POOL_PATCH = get_per_task_callback_patch(
    _report_stats_and_dest, _record_stats,
    '__line_profiler_pool_worker_stats__',
)
get_worker_finalization_patch(_write_recorded_stats, POOL_PATCH)
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


PROCESS_PATCH = SingleModulePatch('multiprocessing.process')
PROCESS_PATCH.add_method('BaseProcess', '_bootstrap', wrap_bootstrap)
