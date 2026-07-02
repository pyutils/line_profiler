"""
Patch :py:mod:`threading` so that profiling extends consistenly into
processes it creates.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar
from typing_extensions import ParamSpec, Concatenate

from ._line_profiler import (  # type: ignore
    USE_LEGACY_TRACE as SHOULD_PATCH_THREADING,
)
from .line_profiler import LineProfiler
from .cleanup import Cleanup


__all__ = ('apply', 'SHOULD_PATCH_THREADING')


T = TypeVar('T')
PS = ParamSpec('PS')

_PATCHED_MARKER = '__line_profiler_patched_threading__'


def make_syncing_wrapper(
    func: Callable[PS, T], prof: LineProfiler, enable_count: int,
) -> Callable[PS, T]:
    """
    Wrap the callable ``func`` so that when we spin up a new thread, we
    sync the
    :py:attr:`line_profiler.line_profiler.LineProfiler.enable_count`  of
    the active profiler (stored at the cache instance loaded from
    :py:meth:`LineProfilingCache.load`) with ``enable_count``.

    Note:
        This only seems to work as intended when using the legacy trace
        system...
    """
    @wraps(func)
    def wrapper(*args: PS.args, **kwargs: PS.kwargs) -> T:
        if TYPE_CHECKING:
            assert hasattr(prof, 'enable_count')
            assert isinstance(prof.enable_count, int)
        # Note: `prof.enable_count` is most likely to be zero on the new
        # thread
        thread_enable_count: int = prof.enable_count
        for _ in range(enable_count - thread_enable_count):
            prof.enable_by_count()
        try:
            return func(*args, **kwargs)
        finally:
            # Reset enable counts to avoid problems if the thread id is
            # ever reused
            for _ in range(prof.enable_count - thread_enable_count):
                prof.disable_by_count()

    return wrapper


def make_thread_init_wrapper(
    prof: LineProfiler,
    vanilla_impl: Callable[
        Concatenate[threading.Thread, None, Callable[..., Any] | None, PS],
        None
    ],
) -> Callable[
    Concatenate[threading.Thread, None, Callable[..., Any] | None, PS], None
]:
    """
    Wrap the initializer of :py:class:`threading.Thread` so that the
    profiler's :py:attr:`LineProfiler.enable_count` is synced up on
    newly spun-up threads.
    """
    @wraps(vanilla_impl)
    def wrapper(
        self: threading.Thread,
        group: None = None,
        target: Callable[..., Any] | None = None,
        *args: PS.args,
        **kwargs: PS.kwargs
    ) -> None:
        enable_count: int | None = getattr(prof, 'enable_count', None)
        if target is not None and enable_count:
            if TYPE_CHECKING:
                assert prof is not None
            target = make_syncing_wrapper(target, prof, enable_count)
        vanilla_impl(self, group, target, *args, **kwargs)

    return wrapper


def apply(cleanup: Cleanup, prof: LineProfiler) -> None:
    """
    Set up profiling in threads started by :py:mod:`threading` by
    applying patches to the module.

    Args:
        cleanup (Cleanup)
            Cleanup instance managing the profiling session

    Side effects:
        - :py:mod:`threading` marked as having been set up

        - The following methods and functions patched:

          - :py:meth:`threading.Thread.__init__`

        - Cleanup callbacks registered via ``cleanup.add_cleanup()``

    Note:
        This is a no-op when using :py:mod:`sys.monitoring`-based
        profiling.
    """
    if not SHOULD_PATCH_THREADING:
        return
    if getattr(threading, _PATCHED_MARKER, False):
        return
    init_wrapper = make_thread_init_wrapper(prof, threading.Thread.__init__)
    cleanup.patch(threading.Thread, '__init__', init_wrapper)
    cleanup.patch(threading, _PATCHED_MARKER, True)
