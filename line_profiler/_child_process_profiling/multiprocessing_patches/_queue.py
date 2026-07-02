from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


__all__ = ('Queue', 'PutWrapper')


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
    ``.put()`` is preceded by calling a ``callback()``; its result is
    optionally attached to the tuple pushed back to the parent if
    ``push_to_parent`` is true.
    """
    def __init__(
        self,
        queue: Queue,
        callback: Callable[[], Any],
        push_to_parent: bool = False,
    ) -> None:
        self._queue = queue
        self._callback = callback
        self._push = push_to_parent

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._queue, attr)

    def put(self, obj: Any) -> None:
        data = self._callback()
        if self._push:
            obj = data, obj
        self._queue.put(obj)

    def get(self) -> Any:
        return self._queue.get()
