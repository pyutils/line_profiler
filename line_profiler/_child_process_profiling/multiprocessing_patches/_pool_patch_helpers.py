from __future__ import annotations

import warnings
from collections.abc import Callable
from functools import partial
from multiprocessing.pool import Pool
from multiprocessing.process import BaseProcess
from typing import Any, TypeVar
from typing_extensions import Concatenate, ParamSpec

from .._patching_infrastructure import SingleModulePatch
from ..cache import LineProfilingCache
from ._queue import Queue, PutWrapper, QuickGetWrapper


__all__ = ('get_per_task_callback_patch', 'get_worker_finalization_patch')

T = TypeVar('T')
P = TypeVar('P', bound=BaseProcess)
PS = ParamSpec('PS')

_POOL_PATCHES: set[str] = set()


def get_per_task_callback_patch(
    get_data: Callable[[LineProfilingCache], T],
    process_data: Callable[[LineProfilingCache, T], Any],
    tag: str,
    patch: SingleModulePatch | None = None,
) -> SingleModulePatch:
    """
    Create a patch for :py:mod:`multiprocessing.pool` which:

    - Patches :py:func:`multiprocessing.pool.worker` so that extra data
      are created in child/worker processes after running EACH task by
      ``get_data(cache)``, and pushed back to  parent process alongside
      said task's result.

    - Patches :py:meth:`multiprocessing.pool.Pool._handle_results` so
      that said extra data is, where possible, retrieved from
      interprocess communication and processed by the parent's active
      :py:class:`.LineProfilingCache` instance.
    """
    if tag in _POOL_PATCHES:
        raise RuntimeError(f'tag {tag!r} already in use')
    _POOL_PATCHES.add(tag)

    wrap_quick_get = partial(QuickGetWrapper, callback=process_data, tag=tag)

    @LineProfilingCache._method_wrapper
    def wrap_handle_results(
        cache: LineProfilingCache,
        vanilla_impl: Callable[
            Concatenate[Queue, Callable[[], tuple[Any, ...] | None], PS],
            None
        ],
        outqueue: Queue,
        # Since we patched `outqueue.put()` in the child process, the
        # result pushed to the parent is (normally) a `(tag, data, obj)`
        # triplet
        get: Callable[[], tuple[Any, ...] | None],
        *args: PS.args,
        **kwargs: PS.kwargs
    ) -> None:
        """
        Wrap around :py:meth:`multiprocessing.pool.Pool._handle_results`
        so that it handles the extra info (result of calling
        ``get_data()`` in a child process after each task) included by
        ``wrap_worker()`` with ``process_data(cache, data)``.

        Note:
            :py:meth:`multiprocessing.pool.Pool._handle_results` is a
            static method.
        """
        vanilla_impl(outqueue, wrap_quick_get(cache, get), *args, **kwargs)

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
        processes attach the result of ``get_data()`` as they pass the
        task results back to the parent.

        Note:
            This is only called in child processes and thus we can't
            reliably measure coverage thereon, hence the ``# nocover``.
        """
        outqueue = PutWrapper(outqueue, partial(get_data, cache), tag)
        return vanilla_impl(inqueue, outqueue, *args, **kwargs)

    if patch is None:
        patch = SingleModulePatch('multiprocessing.pool')
    _check_patch(patch)
    patch.add_method('', 'worker', wrap_worker)
    patch.add_method('Pool', '_handle_results', wrap_handle_results, 'static')
    return patch


def get_worker_finalization_patch(
    process_worker: Callable[[LineProfilingCache, BaseProcess], Any],
    patch: SingleModulePatch | None = None,
) -> SingleModulePatch:
    """
    Create a patch for :py:mod:`multiprocessing.pool` which patches
    :py:meth:`multiprocessing.pool.Pool._terminate_pool` and
    :py:meth:`multiprocessing.pool.Pool._join_exited_workers` so that
    when worker processes are finalized a callback is run.
    """
    @LineProfilingCache._method_wrapper
    def wrap_terminate_pool(
        cache: LineProfilingCache,
        vanilla_impl: Callable[
            Concatenate[type[Pool], Queue, Queue, Queue, list[P], PS], None
        ],
        cls: type[Pool],
        taskqueue: Queue,
        inqueue: Queue,
        outqueue: Queue,
        pool: list[P],
        *args: PS.args,
        **kwargs: PS.kwargs
    ) -> None:
        """
        Wrap around :py:meth:`.Pool._terminate_pool` so that we run
        ``process_worker()`` on finished worker processes.

        Note:
            :py:meth:`.Pool._terminate_pool` is a class method.
        """
        try:
            vanilla_impl(
                cls, taskqueue, inqueue, outqueue, pool, *args, **kwargs,
            )
        finally:
            # Guard against dummy pool; see similar code in
            # `multiprocessing.pool`
            if pool and hasattr(pool[0], 'terminate'):
                failures: list[P] = []
                for worker in pool:
                    if worker.is_alive():  # nocover
                        failures.append(worker)
                        continue
                    process_worker(cache, worker)
                if failures:  # nocover
                    msg = (
                        f'{len(failures)} worker(s) still alive after '
                        f'`Pool.terminate()`: {failures!r}'
                    )
                    cache._debug_output(msg, 'warning')
                    warnings.warn(msg)

    @LineProfilingCache._method_wrapper
    def wrap_join_exited_workers(
        cache: LineProfilingCache,
        vanilla_impl: Callable[[list[P]], bool],
        pool: list[P],
    ) -> bool:
        """
        Wrap around :py:meth:`.Pool._join_exited_workers` so that we run
        ``process_worker()`` on finished worker processes.

        Note:
            :py:meth:`.Pool._join_exited_workers` is a static method.
        """
        before: dict[int, P] = {id(p): p for p in pool}
        if not vanilla_impl(pool):  # No workers cleaned up
            return False
        after: dict[int, P] = {id(p): p for p in pool}
        for i, worker in before.items():
            if after.get(i) is not worker:  # Worker removed from `pool`
                process_worker(cache, worker)
        return True

    if patch is None:
        patch = SingleModulePatch('multiprocessing.pool')
    _check_patch(patch)
    add = partial(patch.add_method, 'Pool')
    add('_terminate_pool', wrap_terminate_pool, 'class')
    add('_join_exited_workers', wrap_join_exited_workers, 'static')
    return patch


def _check_patch(patch: SingleModulePatch) -> None:
    if patch.module == 'multiprocessing.pool':
        return
    msg = (
        'patch = {0!r}, .module = {0.module!r}: '
        'expected patch target to be `multiprocessing.pool`'
    )
    raise AssertionError(msg.format(patch))
