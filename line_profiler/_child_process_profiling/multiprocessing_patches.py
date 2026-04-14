"""
Patch :py:mod:`multiprocessing` so that profiling extends into processes
it creates.

Notes
-----
- Based on the implementations in :py:mod:`coverage.multiproc` and
  :py:mod:`pytest_autoprofile._multiprocessing`.
- Results may vary if the process pool is not properly
  :py:meth:`multiprocessing.pool.Pool.close`-d and
  :py:meth:`multiprocessing.pool.Pool.join`-ed;
  see `this caveat <https://coverage.readthedocs.io/\
en/latest/subprocess.html#using-multiprocessing>`__.
"""
from __future__ import annotations

import multiprocessing
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from functools import partial, wraps
from importlib import import_module
from multiprocessing.process import BaseProcess
from pathlib import Path
from time import sleep
from typing import Any, TypeVar
from typing_extensions import Concatenate, ParamSpec, Self

from .cache import LineProfilingCache
from .pth_hook import _setup_in_child_process
from .runpy_patches import create_runpy_wrapper


__all__ = ('apply',)


S = TypeVar('S')
T = TypeVar('T')
PS = ParamSpec('PS')

_PATCHED_MARKER = '_line_profiler_patched_multiprocessing'
_PROCESS_TERM_LOCK_LOC = '_line_profiler_process_terminate_lock'
_POLLING_COOLDOWN = 1. / 32  # Seconds
# NOTE: Set this to `None` or `True` to tee the `multiprocessing`
# internal logging messages to the log files; if `None`, logs are only
# written if `LineProfilingCache.load().debug` is set to true.
_INTERCEPT_MP_LOG_MESSAGES = False


class PickleHook:
    """
    Object which, when unpickled, sets up profiling in the
    :py:mod:`multiprocessing`-created process.

    See also:
        :py:class:`coverage.multiproc.Stowaway`
    """
    def __getstate__(_) -> int:
        # Cannot return `None`, or nothing will be pickled and
        # `.__getstate__()` will not be invoked in the child
        return 1

    def __setstate__(*_) -> None:
        # We're in a child process created by `multiprocessing`, so set
        # up shop here.
        lp_cache = LineProfilingCache.load()
        _setup_in_child_process(lp_cache, False, 'multiprocessing')
        # In a child process, we don't care about polluting the
        # `multiprocessing` namespace, so don't bother with cleanup
        if not getattr(multiprocessing, _PATCHED_MARKER, False):
            _apply_mp_patches(lp_cache, _no_op)


class _Poller:
    """
    Poll a callable until it returns true-y.
    """
    def __init__(
        self, func: Callable[[], Any], cooldown: float | None = None,
    ) -> None:
        if not (cooldown and cooldown > 0):
            cooldown = 0
        self._func: Callable[[], Any] = func
        self._cooldown = cooldown

    def sleep(self):
        cd = self._cooldown
        if cd > 0:
            sleep(cd)

    def with_cooldown(self, cooldown: float | None) -> Self:
        return type(self)(self._func, cooldown)

    @classmethod
    def poll_until(
        cls, func: Callable[PS, Any], /, *args: PS.args, **kwargs: PS.kwargs
    ) -> Self:
        if args or kwargs:
            func = partial(func, *args, **kwargs)
        return cls(func)

    @classmethod
    def poll_while(
        cls, func: Callable[PS, Any], /, *args: PS.args, **kwargs: PS.kwargs
    ) -> Self:
        def negated(
            func: Callable[PS, Any], *a: PS.args, **k: PS.kwargs
        ) -> bool:
            return not func(*a, **k)

        return cls(partial(negated, func, *args, **kwargs))

    def __enter__(self) -> Self:
        while not self._func():
            self.sleep()
        return self

    def __exit__(self, *_, **__) -> None:
        pass


def _method_wrapper(
    wrapper: Callable[Concatenate[S, Callable[Concatenate[S, PS], T], PS], T]
) -> Callable[
    [Callable[Concatenate[S, PS], T]], Callable[Concatenate[S, PS], T]
]:
    def inner_wrapper(
        vanilla_impl: Callable[Concatenate[S, PS], T],
    ) -> Callable[Concatenate[S, PS], T]:
        @wraps(vanilla_impl)
        def wrapped_impl(self: S, *a: PS.args, **k: PS.kwargs) -> T:
            return wrapper(self, vanilla_impl, *a, **k)

        return wrapped_impl

    for field in 'name', 'qualname', 'doc':
        dunder = f'__{field}__'
        value = getattr(wrapper, dunder, None)
        if value is not None:
            setattr(inner_wrapper, dunder, value)
    return inner_wrapper


@_method_wrapper
def wrap_start(
    self: BaseProcess, vanilla_impl: Callable[[BaseProcess], None],
) -> None:
    """
    Wrap around :py:meth:`BaseProcess.start` to specify the location for
    a lock file, which is managed by the child process and checked by
    the parent. This is to ensure that the child can exit gracefully and
    complete any necessary cleanup.
    """
    cache = LineProfilingCache.load()
    tempfile = cache.make_tempfile(prefix='process-term-lock-', suffix='.lock')
    setattr(self, _PROCESS_TERM_LOCK_LOC, tempfile)
    vanilla_impl(self)


@_method_wrapper
def wrap_terminate(
    self: BaseProcess, vanilla_impl: Callable[[BaseProcess], None],
) -> None:
    """
    Wrap around :py:meth:`BaseProcess.terminate` to make sure that we
    don't actually kill the child (OS-level) process before it has the
    chance to properly clean up. This is done by blocking the call as
    long as a lock file exists, which is specified by the parent process
    and managed by the child.

    Note:
        We're technically polling in a hot loop, but:

        - We're only calling this when explicitly terminating processes,
          which isn't that bad; and

        - Such calls typically only happen:

          - When e.g. the parallel function exectued in child
            processes raised an error, so we're already on a "bad"
            path; and

          - AFTER the performance-critical part of the code (the
            parallelly-run function).

        To circumvent this we may use dedicated FS-watching APIs like
        :py:mod:`watchdog` (which use syscalls to do this), but we'll
        think about introducing extra dependencies when we REALLY have
        to.
    """
    # XXX: why can `coverage` get away with not doing all this lock-file
    # hijinks and just patching `BaseProcess._bootstrap()`?
    lock_file: Path | None = getattr(self, _PROCESS_TERM_LOCK_LOC, None)
    if lock_file:
        lock: AbstractContextManager[Any] = (
            _Poller.poll_while(lock_file.exists)
            .with_cooldown(_POLLING_COOLDOWN)
        )
    else:
        lock = nullcontext()
    with lock:
        try:
            delattr(self, _PROCESS_TERM_LOCK_LOC)
        except AttributeError:
            pass
        vanilla_impl(self)


@_method_wrapper
def wrap_bootstrap(
    self: BaseProcess,
    vanilla_impl: Callable[Concatenate[BaseProcess, PS], T], /,
    *args: PS.args, **kwargs: PS.kwargs
) -> T:
    """
    Wrap around :py:meth:`BaseProcess._bootstrap` to:

    - Run ``LineProfilingCache.load().cleanup()`` so that profiling
      results can be gathered; and

    - Write a lock file before executing ``vanilla_impl()`` and deleted
      it thereafter, to ensure that a parant process doesn't prematurely
      ``.terminate()`` a failed child before the profiling results can
      be gathered.
    """
    cache = LineProfilingCache.load()
    lock_file: Path | None = getattr(self, _PROCESS_TERM_LOCK_LOC, None)

    if lock_file:
        lock_file.touch()
        cache.add_cleanup(lock_file.unlink, missing_ok=True)
    try:
        return vanilla_impl(self, *args, **kwargs)
    finally:
        cache._debug_output(
            'Calling cleanup hook via `BaseProcess._bootstrap`'
        )
        cache.cleanup()


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
    vanilla_impl: Callable[Concatenate[str, PS], None],
    marker: str,
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


def get_preparation_data(
    vanilla_impl: Callable[PS, dict[str, Any]],
    /,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> dict[str, Any]:
    """
    Wrap around :py:func:`multiprocessing.spawn.get_preparation_data`,
    slipping a :py:class:`PickleHook` into the returned dictionary so
    that profiling is triggered upon unpickling.

    Args:
        vanilla_impl
            Vanilla
            :py:func:`multiprocessing.spawn.get_preparation_data`
        *args
        **kwargs
            Passed to
            :py:func:`multiprocessing.spawn.get_preparation_data`

    Returns
        Dictionary returned by
        ``get_preparation_data(*args, **kwargs)`` with an extra key
    """
    key = 'line_profiler_pickle_hook'  # Doesn't matter
    data = vanilla_impl(*args, **kwargs)
    assert key not in data
    data[key] = PickleHook()
    return data


def apply(lp_cache: LineProfilingCache) -> None:
    """
    Set up profiling in :py:mod:`multiprocessing` child processes by
    applying patches to the module.

    Args:
        lp_cache (LineProfilingCache)
            Cache instance governing the profiling run

    Side effects:
        - :py:mod:`multiprocessing` marked as having been set up

        - The following methods and functions patched:
          - :py:meth:`multiprocessing.process.BaseProcess.start`

          - :py:meth:`multiprocessing.process.BaseProcess.terminate`

          - :py:meth:`multiprocessing.process.BaseProcess._bootstrap`

          - :py:func:`multiprocessing.spawn.get_preparation_data`

        - Cleanup callbacks registered via ``lp_cache.add_cleanup()``
    """
    if not getattr(multiprocessing, _PATCHED_MARKER, False):
        _apply_mp_patches(lp_cache, lp_cache.add_cleanup)


def _apply_mp_patches(
    lp_cache: LineProfilingCache,
    add_cleanup: Callable[..., Any],
    debug: bool | None = _INTERCEPT_MP_LOG_MESSAGES,
) -> None:
    def replace(
        obj: Any, attr: str, value: Any, obj_name: str | None = None,
    ) -> None:
        try:
            old = getattr(obj, attr)
        except AttributeError:
            add_cleanup(delattr, obj, attr)
        else:
            add_cleanup(setattr, obj, attr, old)
        setattr(obj, attr, value)
        if obj_name is None:
            obj_name = repr(obj)
        lp_cache._debug_output('Patched `{}.{}` -> `{}`'.format(
            obj_name, attr, value,
        ))

    # Patch `multiprocessing.process.BaseProcess` methods
    Method = Callable[Concatenate[S, PS], T]
    patches: dict[str, Callable[[Method], Method]]
    for submodule, target, patches in [  # type: ignore[assignment]
        ('process', 'BaseProcess', {
            'start': wrap_start,
            'terminate': wrap_terminate,
            '_bootstrap': wrap_bootstrap,
        }),
    ]:
        try:
            mod = import_module('multiprocessing.' + submodule)
        except ImportError:
            continue
        Class = getattr(mod, target)
        name = f'{Class.__module__}.{Class.__qualname__}'
        for method, method_wrapper in patches.items():
            vanilla = getattr(Class, method)
            replace(Class, method, method_wrapper(vanilla), name)

    # Patch `multiprocessing.spawn`
    try:
        from multiprocessing import spawn
    except ImportError:  # Incompatible platforms
        pass
    else:
        # Patch `get_preparation_data()`
        gpd_wrapper = partial(  # type: ignore[call-arg]
            get_preparation_data, spawn.get_preparation_data,
        )
        replace(spawn, 'get_preparation_data', gpd_wrapper, spawn.__name__)
        # Patch `runpy` (do it locally instead of tempering with the
        # global `runpy` mmodule)
        if hasattr(spawn, 'runpy'):
            runpy_wrapper = create_runpy_wrapper(lp_cache)
            replace(spawn, 'runpy', runpy_wrapper, spawn.__name__)

    # Intercept `multiprocessing` debug messages
    if debug is None:
        debug = lp_cache.debug
    if debug:
        from multiprocessing import util

        for logging_func in [
            'sub_debug', 'debug', 'info', 'sub_warning', 'warn',
        ]:
            try:
                vanilla = getattr(util, logging_func)
            except AttributeError:
                continue
            replace(
                util, logging_func, partial(tee_log, vanilla, logging_func),
                'multiprocessing.util',
            )

    # Mark `multiprocessing` as having been patched
    replace(multiprocessing, _PATCHED_MARKER, True)


def _no_op(*_, **__) -> None:
    pass
