from __future__ import annotations

import os
from collections.abc import Callable
from functools import partial, wraps
from multiprocessing.process import BaseProcess
from types import MethodType
from typing import Any, TypeVar, cast
from typing_extensions import Concatenate, ParamSpec

from ..cache import LineProfilingCache
from ._infrastructure import SingleModulePatch
from ._queue import Queue, PutWrapper


__all__ = (
    'CHILD_PIDS_PATCH', 'LOGGING_PATCH',
    'tee_log', 'wrap_handle_results', 'wrap_process', 'wrap_worker',
)

T = TypeVar('T')
P = TypeVar('P', bound=BaseProcess)
PS = ParamSpec('PS')

_LOGGERS = ['sub_debug', 'debug', 'info', 'sub_warning', 'warn']

# ---------------------- PID bookkeeping patches -----------------------


@LineProfilingCache._method_wrapper
def wrap_handle_results(
    cache: LineProfilingCache,
    vanilla_impl: Callable[
        Concatenate[Queue, Callable[[], tuple[Any, ...] | None], PS],
        None
    ],
    outqueue: Queue,
    # Since we patched `outqueue.put()` in the child process, the result
    # tuple pushed to the parent has an extra item (the child PID)
    get: Callable[[], tuple[int, tuple[Any, ...]] | None],
    *args: PS.args,
    **kwargs: PS.kwargs
) -> None:
    """
    Wrap around :py:meth:`multiprocessing.pool.Pool._handle_results` so
    that it handles the extra info (PID of child process handling the
    task) included by :py:func:`.wrap_worker`.

    Note:
        :py:meth:`.Pool._handle_results` is a static method.
    """
    # Somehow this doesn't type-check with either `mypy` or `ty` when
    # we use a `TypeVar` instead of `Any` with the tuple items...
    # (see `ty` issue #3467)
    wrapped_get = partial(_wrap_outqueue_quick_get, cache, get)
    vanilla_impl(outqueue, wrapped_get, *args, **kwargs)


@LineProfilingCache._method_wrapper  # nocover
def wrap_worker(
    _,  # We don't need the cache instance, but `@_method_wrapper` does
    vanilla_impl: Callable[Concatenate[Queue, Queue, PS], None],
    inqueue: Queue,
    outqueue: Queue,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> None:
    """
    Wrap around :py:func:`multiprocessing.pool.worker` so that child
    processes report their PIDs as they pass the task results back to
    the parent.

    Note:
        This is only called in child processes and thus we can't
        reliably measure coverage thereon; see also
        :py:func:`wrap_bootstrap`.
    """
    outqueue = PutWrapper(outqueue, os.getpid, push_to_parent=True)
    return vanilla_impl(inqueue, outqueue, *args, **kwargs)


@LineProfilingCache._method_wrapper
def wrap_process(
    cache: LineProfilingCache,
    vanilla_impl: Callable[PS, P],
    *args: PS.args,
    **kwargs: PS.kwargs
) -> P:
    """
    Wrap around :py:func:`multiprocessing.pool.Pool.Process` so that the
    processes created can report on usage when
    :py:meth:`.BaseProcess.join`-ed or
    :py:meth:`.BaseProcess.terminate`-ed.

    Note:
        :py:meth:`.Pool.Process` is a static method.
    """
    proc = vanilla_impl(*args, **kwargs)
    # Note: since we don't clean up here, there's no need to instantiate
    # another `Cleanup` helper
    name = f'<{type(proc).__name__} @ {hex(id(proc))}>'
    patch = partial(cache.patch, cleanup=False, name=name)
    for method, action in ('join', 'joining'), ('terminate', 'terminating'):
        bound = getattr(proc, method)
        assert isinstance(bound, MethodType)
        finalize = _wrap_process_finalize(cache, bound.__func__, action)
        patch(proc, method, MethodType(finalize, proc))
    return proc


def _wrap_process_finalize(
    cache: LineProfilingCache,
    vanilla_impl: Callable[Concatenate[P, PS], None],
    action: str,
) -> Callable[Concatenate[P, PS], None]:
    """
    Check if the process has run any tasks;
    if not, report to the cache.

    Note:
        Since the process object is pickled, this method has to directly
        return a function object instead of merely being
        :py:func:`partial`-ed and wrapped in a
        :py:class:`types.MethodType`.
    """
    @wraps(vanilla_impl)
    def finalize(self: P, *args: PS.args, **kwargs: PS.kwargs) -> None:
        log = cache._debug_output
        call = cache._format_call(vanilla_impl, self, *args, **kwargs)
        try:
            log(f'Wrapped call made: {call}')
            pid: int | None = getattr(self, 'pid', None)
            checked_procs = _get_checked_processes(cache)
            identifier = id(self), pid
            if not (pid is None or identifier in checked_procs):
                ntasks = _get_ntasks(cache).pop(pid, 0)
                if not ntasks:
                    cache._warn_possible_lack_of_stats(pid)
                log(f'{action} process {pid} which ran {ntasks} task(s)...')
                checked_procs.add(cast(tuple[int, int], identifier))
        except BaseException as e:
            log(
                f'Error in bookkeeping ({cache._format_exception(e)}), '
                'invoking base implementation nonetheless...'
            )
            raise e
        finally:
            try:
                vanilla_impl(self, *args, **kwargs)
            except BaseException as e:
                state = f'failed ({cache._format_exception(e)})'
                raise e
            else:
                state = 'succeeded'
            finally:
                log(f'Wrapped call {call} {state}')

    action = action.capitalize()
    return finalize


def _wrap_outqueue_quick_get(
    cache: LineProfilingCache,
    vanilla_impl: Callable[PS, tuple[int, tuple[Any, ...]] | None],
    *args: PS.args,
    **kwargs: PS.kwargs
) -> tuple[Any, ...] | None:
    """
    Take and process the PID of the child process completing the task.
    """
    result = vanilla_impl(*args, **kwargs)
    if result is None:
        return None
    pid, orig_result = result
    ntasks = _get_ntasks(cache)
    ntasks[pid] = ntasks.get(pid, 0) + 1
    return orig_result


def _get_ntasks(cache: LineProfilingCache) -> dict[int, int]:
    key = 'mp_proc_ntasks'
    return cache._additional_data.setdefault(key, cast(dict[int, int], {}))


def _get_checked_processes(
    cache: LineProfilingCache,
) -> set[tuple[int, int]]:
    key = 'mp_proc_checked_workload'
    return cache._additional_data.setdefault(
        key, cast(set[tuple[int, int]], set()),
    )


CHILD_PIDS_PATCH = (
    SingleModulePatch('pool')
    .add_method('', 'worker', wrap_worker)
    .add_method('Pool', '_handle_results', wrap_handle_results, 'static')
    .add_method('Pool', 'Process', wrap_process, 'static')
)

# --------------- `multiprocessing.util` logging patches ---------------


def _cache_hook(
    vanilla_impl: Callable[PS, T],
    get_logging_message: Callable[PS, str],
    /,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> T:
    msg = get_logging_message(*args, **kwargs)
    LineProfilingCache.load()._debug_output(msg)
    return vanilla_impl(*args, **kwargs)


def tee_log(
    marker: str,
    vanilla_impl: Callable[Concatenate[str, PS], None],
    /,
    msg: str,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> None:
    """
    Wrap around logging functions like
    :py:func:`multiprocessing.util.debug` so that we can tee log
    messages from the package to our own logs.
    """
    def get_msg(msg: str, *_, **__) -> str:
        return f'`multiprocessing` logging ({marker}): {msg}'

    _cache_hook(
        vanilla_impl, get_msg,  # type: ignore[arg-type]
        msg, *args, **kwargs,
    )


LOGGING_PATCH = SingleModulePatch('util').add_target(
    # The logging functions exists directly in the module namespace so
    # no further attribute access is needed
    '', {func: partial(partial, tee_log, func) for func in _LOGGERS},
)
