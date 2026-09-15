from __future__ import annotations

import warnings
from collections.abc import Callable
from functools import partial
from typing import Any, Generic, Protocol, TypeVar, cast
from typing_extensions import ParamSpec

from ...cleanup import _CALLBACK_REPR
from ..cache import LineProfilingCache


__all__ = ('Queue', 'PutWrapper', 'QuickGetWrapper')

T = TypeVar('T')
PS = ParamSpec('PS')

_UNTAGGED_RESULT_WARNING_TEMPLATE = (
    'received a pool-task result `{result}` without the expected tag {tag!r}; '
    'the worker process appears not to have been set up for profiling '
    '(e.g. its interpreter never loaded the profiling startup hook), '
    'so its profiling data will be missing from the output'
)


class Queue(Protocol):
    """
    Protocol for methods common to e.g. :py:class:`queue.SimpleQueue`
    and :py:class:`multiprocessing.queues.SimpleQueue`.
    """
    def put(self, obj: Any) -> None:
        ...

    def get(self) -> Any:
        ...


class PutWrapper:
    """
    Wrap around a queue (the ``outqueue`` argument to
    :py:func:`multiprocessing.pool.worker`) so that each call to its
    ``.put()`` is preceded by calling a ``callback()``; if
    ``tag`` is given, the object pushed to the parent is replaced with
    the triplet ``(tag, callback(), obj)``.
    """
    def __init__(
        self,
        queue: Queue,
        callback: Callable[[], Any],
        tag: str | None = None,
    ) -> None:
        self._queue = queue
        self._callback = callback
        self._tag = tag

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._queue, attr)

    def put(self, obj: Any) -> None:
        data = self._callback()
        if self._tag is not None:
            obj = self._tag, data, obj
        self._queue.put(obj)

    def get(self) -> Any:
        return self._queue.get()


class QuickGetWrapper(Generic[PS, T]):
    """
    Wrap around a :py:attr:`multiprocessing.pool.Pool._quick_get` to
    intercept and process data slipped into the queue by a
    :py:class:`PutWrapper`.

    - If the result of the get is :py:const:`None` (i.e. a sentinel
      value), it is a no-op.

    - If the result is a 3-tuple consisting of the ``tag``, some
      ``data``, followed by the original result, the ``data`` is
      processed with
      ``callback(cache: LineProfilingCache, data: T) -> Any`` and the
      original result is returned.

    - Otherwise, a warning (once per ``tag``) is emitted and the result
      is returned as-is.
    """
    def __init__(
        self,
        cache: LineProfilingCache,
        get: Callable[PS, tuple[Any, ...] | None],
        callback: Callable[[LineProfilingCache, T], Any],
        tag: str,
    ) -> None:
        self._impl = get
        self._callback = partial(callback, cache)
        self._warn = partial(self._warn_untagged_result_once, cache, tag)
        self._tag = tag

    def __call__(
        self, /, *args: PS.args, **kwargs: PS.kwargs
    ) -> tuple[Any, ...] | None:
        """
        Note:
            A worker which was never patched (its interpreter didn't run
            the profiling startup hook) pushes vanilla un-tagged
            results; those are passed through untouched, with a
            once-per-session warning, so that a mixed
            patched-parent/vanilla-worker setup degrades to missing
            profile data instead of killing the pool's result-handler
            thread (and thereby deadlocking every
            :py:meth:`multiprocessing.pool.AsyncResult.get`).
        """
        result = self._impl(*args, **kwargs)
        if result is None:
            return None
        if (
            isinstance(result, tuple)
            and len(result) == 3
            and result[0] == self._tag
        ):
            _, data, orig_result = result
            self._callback(cast(T, data))
            return orig_result
        self._warn(result)
        return result

    @staticmethod
    def _warn_untagged_result_once(
        cache: LineProfilingCache, tag: str, result: Any,
    ) -> None:
        key = 'mp_result_handler_untagged_result_warnings'
        # No lock: a race just means an extra warning, and this runs on
        # the pool's single result-handler thread anyway
        has_warned = cache._additional_data.setdefault(key, {})
        if has_warned.get(tag):
            return
        has_warned[tag] = True
        # Log before warning in case the warning is promoted to an error
        msg = _UNTAGGED_RESULT_WARNING_TEMPLATE.format(
            result=_CALLBACK_REPR(result), tag=tag,
        )
        cache._debug_output(msg, 'warning')
        warnings.warn(msg)
