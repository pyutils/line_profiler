"""
Patch :py:mod:`threading` so that profiling extends consistenly into
processes it creates.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Collection, Mapping
from functools import wraps
from types import MethodType, ModuleType
from typing import TYPE_CHECKING, Any, TypeVar, cast, overload
from typing_extensions import ParamSpec, Concatenate

from .line_profiler import LineProfiler
from .cleanup import Cleanup


__all__ = ('apply',)


T = TypeVar('T')
PS = ParamSpec('PS')

_PATCHED_MARKER = '__line_profiler_patched_threading__'


@overload
def make_syncing_wrapper(
    func: Callable[PS, T], prof_enable_counts: Mapping[LineProfiler, int],
) -> Callable[PS, T]:
    ...


@overload
def make_syncing_wrapper(
    func: MethodType, prof_enable_counts: Mapping[LineProfiler, int],
) -> MethodType:
    ...


def make_syncing_wrapper(
    func: Callable[PS, T] | MethodType,
    prof_enable_counts: Mapping[LineProfiler, int],
) -> Callable[PS, T] | MethodType:
    """
    Wrap the callable ``func`` so that when we spin up a new thread, we
    sync the
    :py:attr:`line_profiler.line_profiler.LineProfiler.enable_count`s of
    the profilers as specified by ``prof_enable_counts``.

    Example:
        >>> class Cls:
        ...     @classmethod
        ...     def method(cls) -> int:
        ...         print(cls.__name__, prof.enable_count)

        >>> prof = LineProfiler(Cls.method)
        >>> Cls.method()
        Cls 0
        >>> assert not any(
        ...     (stats := prof.get_stats()).timings.values()
        ... ), stats

        >>> func_wrapper = make_syncing_wrapper(Cls.method, {prof: 2})
        >>> prof.enable_count
        0
        >>> func_wrapper()
        Cls 2
        >>> prof.enable_count
        0
        >>> assert any(
        ...     (stats := prof.get_stats()).timings.values()
        ... ), stats
    """
    # Note: the above doctest is mainly here for coverage purposes,
    # since this function is otherwise only called on a thread in a part
    # inaccessible to `coverage`.
    if isinstance(func, MethodType):
        impl = make_syncing_wrapper(func.__func__, prof_enable_counts)
        return MethodType(impl, func.__self__)

    @wraps(func)
    def wrapper(*args: PS.args, **kwargs: PS.kwargs) -> T:
        for prof, enable_count in prof_enable_counts.items():
            _sync_enable_count(prof, enable_count)
        try:
            return func(*args, **kwargs)
        finally:
            # Reset enable counts to avoid problems if the "physical"
            # thread id is ever reused
            for prof in prof_enable_counts:
                try:
                    _sync_enable_count(prof, 0)
                except Exception:
                    pass

    return wrapper


def _sync_enable_count(prof: LineProfiler, count: int) -> None:
    """
    Example:
        (Use a dummy class to avoid the side effects of having live
        :py:class:`LineProfiler` objects at test teardown and/or process
        termination.)

        >>> from collections import Counter
        >>> from typing import cast
        >>> from line_profiler import LineProfiler

        >>> class MockProfiler:
        ...     def __init__(self) -> None:
        ...         self.enable_count = 0
        ...         self._events = Counter()
        ...
        ...     def enable(self) -> None:
        ...         self._events['enable'] += 1
        ...
        ...     def disable(self) -> None:
        ...         self._events['disable'] += 1
        ...
        ...     def enable_by_count(self) -> None:
        ...         if not self.enable_count:
        ...             self.enable()
        ...         self.enable_count += 1
        ...
        ...     def disable_by_count(self) -> None:
        ...         if self.enable_count <= 0:
        ...             return
        ...         if self.enable_count == 1:
        ...             self.disable()
        ...         self.enable_count -= 1

        >>> def test_sync(prof: MockProfiler, count: int) -> None:
        ...     print(f'{prof.enable_count} -> {count}')
        ...     _sync_enable_count(cast(LineProfiler, prof), count)
        ...     assert (
        ...         prof.enable_count == count
        ...     ), f'{prof.enable_count=!r}, {count=!r}'

        >>> prof = MockProfiler()
        >>> test_sync(prof, 4)  # Enabled
        0 -> 4
        >>> test_sync(prof, 6)
        4 -> 6
        >>> test_sync(prof, 1)
        6 -> 1
        >>> test_sync(prof, 3)
        1 -> 3
        >>> test_sync(prof, 0)  # Disabled
        3 -> 0
        >>> test_sync(prof, 5)  # Enabled
        0 -> 5
        >>> assert (
        ...     prof._events == {'enable': 2, 'disable': 1}
        ... ), f'{prof._events=!r}'
    """
    if TYPE_CHECKING:
        assert hasattr(prof, 'enable_count')
        assert isinstance(prof.enable_count, int)
    delta = count - prof.enable_count
    if delta > 0:
        bump_count: Callable[[], None] = prof.enable_by_count
    else:
        bump_count, delta = prof.disable_by_count, -delta
    for _ in range(delta):
        bump_count()


def wrap_thread_start(
    profs: Collection[LineProfiler],
    vanilla_impl: Callable[Concatenate[threading.Thread, PS], None],
) -> Callable[Concatenate[threading.Thread, PS], None]:
    """
    Wrap :py:meth:`threading.Thread.start` so that the profilers'
    :py:attr:`LineProfiler.enable_count`s are synced up on newly spun-up
    threads.
    """
    @wraps(vanilla_impl)
    def wrapper(
        self: threading.Thread, *args: PS.args, **kwargs: PS.kwargs
    ) -> None:
        if TYPE_CHECKING:
            assert hasattr(self, '_bootstrap')
        prof_enable_counts: dict[LineProfiler, int] = {}
        for prof in profs:
            count: int = getattr(prof, 'enable_count', 0)
            if count:
                prof_enable_counts[prof] = count
        if prof_enable_counts:
            bst: Callable[..., Any] | MethodType = cast(Any, self._bootstrap)
            # `.start()` passes `._bootstrap()` to some lower-level
            # function to spin up the new thread.
            bst = wrap_thread_bootstrap(prof_enable_counts, bst)
            # This is a private method; we don't care about restoring it
            self._bootstrap = bst  # type: ignore
        vanilla_impl(self, *args, **kwargs)

    return wrapper


@overload
def wrap_thread_bootstrap(
    prof_enable_counts: Mapping[LineProfiler, int],
    vanilla_impl: Callable[Concatenate[threading.Thread, PS], None],
) -> Callable[Concatenate[threading.Thread, PS], None]:
    ...


@overload
def wrap_thread_bootstrap(
    prof_enable_counts: Mapping[LineProfiler, int], vanilla_impl: MethodType,
) -> MethodType:
    ...


def wrap_thread_bootstrap(
    prof_enable_counts: Mapping[LineProfiler, int],
    vanilla_impl: Callable[
        Concatenate[threading.Thread, PS], None
    ] | MethodType,
) -> Callable[Concatenate[threading.Thread, PS], None] | MethodType:
    """
    Wrap :py:meth:`threading.Thread._bootstrap` so that the profilers'
    :py:attr:`LineProfiler.enable_count` is synced up on newly spun-up
    threads.

    Notes:
        This is separate from :py:func:`wrap_thread_start` because:

        - :py:func:`wrap_thread_start` is responsible for
          capturing ``prof.enable_count`` at startup on the parent
          thread.

        - However, if :py:func:`threading.settrace` is used, the
          supplied callable will override the legacy trace callback mid
          ``._bootstrap()`` (inside
          :py:meth:`threading.Thread._bootstrap_inner`, before calling
          :py:meth:`threading.Thread.run`), thus interfering with
          profiling (when the "legacy" core is used).

        - To circumvent that, we use a wrapper to reversibly
          monkey-patch :py:meth:`threading.Thread.run`, so that profiler
          activation happens after the call to :py:func:`sys.settrace`
          and can thus "wrap" the callable.
    """
    if isinstance(vanilla_impl, MethodType):
        impl = wrap_thread_bootstrap(prof_enable_counts, vanilla_impl.__func__)
        return MethodType(impl, vanilla_impl.__self__)

    @wraps(vanilla_impl)
    def wrapper(
        self: threading.Thread, *args: PS.args, **kwargs: PS.kwargs
    ) -> None:  # nocover
        # Note: this CANNOT be covered because `coverage` uses
        # `threading.settrace()` to set up shop on the new thread, but
        # that is only called INSIDE `._bootstrap_inner()`
        with Cleanup() as cleanup:
            run = make_syncing_wrapper(self.run, prof_enable_counts)
            cleanup.patch(self, 'run', run)
            vanilla_impl(self, *args, **kwargs)

    return wrapper


def apply(
    cleanup: Cleanup,
    profs: LineProfiler | Collection[LineProfiler],
    threading: ModuleType | None = None,
) -> None:
    """
    Set up profiling in threads started by :py:mod:`threading` by
    applying patches to the module.

    Args:
        cleanup (Cleanup):
            :py:class:`Cleanup` instance managing the profiling session.

        profs (LineProfiler | Collection[LineProfiler]):
            :py:class:`LineProfiler` instance(s) used in the session.

        threading (ModuleType | None):
            Optional :py:mod:`threading` module or a copy thereof;
            default is the global module.

    Side effects:
        - :py:mod:`threading` marked as having been set up.

        - The following methods and functions patched:

          - :py:meth:`threading.Thread.start`.

        - Cleanup callbacks registered via ``cleanup.add_cleanup()``.

    Examples:
        >>> from contextlib import ExitStack
        >>> from threading import Thread

        >>> from line_profiler import LineProfiler
        >>> from line_profiler.cleanup import Cleanup

        >>> prof1 = LineProfiler()
        >>> prof2 = LineProfiler()

        >>> def print_enable_counts(header: str | None = None) -> None:
        ...     '''
        ...     Note that these counts are thread-local.
        ...     '''
        ...     if header is None:
        ...         header = ''
        ...     else:
        ...         header = f'{header}: '
        ...     print(f'{header}{prof1.enable_count = !r}')
        ...     print(f'{header}{prof2.enable_count = !r}')

        >>> def print_enable_counts_in_another_thread() -> None:
        ...     thread = Thread(
        ...         target=print_enable_counts, args=('In new thread',),
        ...     )
        ...     thread.start()
        ...     thread.join()

        >>> class enable_profilers:
        ...     '''
        ...     Manage the :py:attr:`LineProfiler.enable_count`s inside
        ...     the context.  NON-REENTRANT.
        ...     '''
        ...     def __init__(
        ...         self, prof1: int = 0, prof2: int = 0,
        ...     ) -> None:
        ...         self.prof1 = prof1
        ...         self.prof2 = prof2
        ...         self._stack: ExitStack | None = None
        ...
        ...     def __enter__(self) -> None:
        ...         pc = [(prof1, self.prof1), (prof2, self.prof2)]
        ...         assert self._stack is None
        ...         stack = self._stack = ExitStack()
        ...         for prof, count in pc:
        ...             for _ in range(count):
        ...                 stack.enter_context(prof)
        ...
        ...     def __exit__(self, *_, **__) -> None:
        ...         assert self._stack is not None
        ...         self._stack.close()
        ...         self._stack = None

        No patches:

        >>> with enable_profilers(1, 2):
        ...     print_enable_counts('In context')
        ...     print_enable_counts_in_another_thread()
        In context: prof1.enable_count = 1
        In context: prof2.enable_count = 2
        In new thread: prof1.enable_count = 0
        In new thread: prof2.enable_count = 0

        >>> print_enable_counts()
        prof1.enable_count = 0
        prof2.enable_count = 0

        Syncing a single profiler instance:

        >>> with ExitStack() as stack:
        ...     stack.enter_context(enable_profilers(1, 2))
        ...     cleanup = stack.enter_context(Cleanup())
        ...     apply(cleanup, prof1)
        ...     print_enable_counts('In context')
        ...     print_enable_counts_in_another_thread()
        In context: prof1.enable_count = 1
        In context: prof2.enable_count = 2
        In new thread: prof1.enable_count = 1
        In new thread: prof2.enable_count = 0

        >>> print_enable_counts()
        prof1.enable_count = 0
        prof2.enable_count = 0

        Syncing multiple profiler instances:

        >>> with ExitStack() as stack:
        ...     stack.enter_context(enable_profilers(1, 2))
        ...     cleanup = stack.enter_context(Cleanup())
        ...     apply(cleanup, [prof1, prof2])
        ...     print_enable_counts('In context')
        ...     print_enable_counts_in_another_thread()
        In context: prof1.enable_count = 1
        In context: prof2.enable_count = 2
        In new thread: prof1.enable_count = 1
        In new thread: prof2.enable_count = 2

        >>> print_enable_counts()
        prof1.enable_count = 0
        prof2.enable_count = 0

    Note:
        Trying to re-apply the patches while existing ones are not
        undone will result in a :py:class:`RuntimeError`:

        >>> with Cleanup(
        ... ) as cleanup:  # doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        ...     apply(cleanup, prof1)
        ...     apply(cleanup, prof2)
        Traceback (most recent call last):
          ...
        RuntimeError: threading=<module 'threading' ...>:
        already patched
    """
    if threading is None:
        threading = cast(ModuleType, globals()['threading'])
    if getattr(threading, _PATCHED_MARKER, False):
        raise RuntimeError(f'{threading=!r}: already patched')

    # Wrap in a `set()` to deduplicate
    if isinstance(profs, Collection):
        profs = set(cast(Collection[LineProfiler], profs))
    else:
        profs = {profs}
    start_wrapper = wrap_thread_start(profs, threading.Thread.start)
    cleanup.patch(threading.Thread, 'start', start_wrapper)
    cleanup.patch(threading, _PATCHED_MARKER, True)
