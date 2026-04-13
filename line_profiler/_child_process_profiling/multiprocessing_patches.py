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

import multiprocessing.process
from collections.abc import Callable
from functools import partial, partialmethod, wraps
from typing import Any, TypeVar
from typing_extensions import Concatenate, ParamSpec

from .cache import LineProfilingCache
from .pth_hook import _setup_in_child_process
from .runpy_patches import create_runpy_wrapper


__all__ = ('apply',)


S = TypeVar('S')
T = TypeVar('T')
PS = ParamSpec('PS')

_PATCHED_MARKER = '_line_profiler_patched_multiprocessing'


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
        if not getattr(multiprocessing, _PATCHED_MARKER, False):
            _apply_mp_patches(lp_cache, True, True)


def cleanup_wrapper(
    vanilla_impl: Callable[PS, T], name: str | None = None,
) -> Callable[PS, T]:
    """
    Wrap around :py:class:`multiprocessing.process.BaseProcess` methods
    like ``._bootstrap()``, writing the profiling results after it is
    run.

    Args:
        vanilla_impl (Callable)
            Vanilla implementation of the method
        name (str | None)
            Optional name to use in debug messages; if not provided, use
            ``vanilla_impl.__name__`` where available.

    Returns:
        Wrapper around ``vanilla_impl``

    Side effects:
        Profiling results are written as the wrapper function exits,
        before the result of ``vanilla_impl()`` is returned

    See also:
        :py:func:`setup_wrapper`
    """
    @wraps(vanilla_impl)
    def wrapper(*args: PS.args, **kwargs: PS.kwargs) -> T:
        try:
            return vanilla_impl(*args, **kwargs)
        finally:  # Write profiling results
            cache = LineProfilingCache.load()
            cache._debug_output(f'Calling cleanup hook via: {name}')
            for msg in 'FOO', 'BAR', 'BAZ':
                cache._debug_output(msg)
            cache.cleanup()

    if name is None:
        name = getattr(vanilla_impl, '__name__', '???')
    return wrapper


def setup_wrapper(
    vanilla_impl: Callable[PS, T], name: str | None = None,
) -> Callable[PS, T]:
    """
    Wrap around :py:class:`multiprocessing.process.BaseProcess` methods
    like ``.start()``, setting up profiling before it is run.

    Args:
        vanilla_impl (Callable)
            Vanilla implementation of the method
        name (str | None)
            Optional name to use in debug messages; if not provided, use
            ``vanilla_impl.__name__`` where available.

    Returns:
        Wrapper around ``vanilla_impl``

    Side effects:
        Profiling set up when the wrapper function is called, before
        ``vanilla_impl`` itself is invoked

    See also:
        :py:func:`cleanup_wrapper`
    """
    @wraps(vanilla_impl)
    def wrapper(*args: PS.args, **kwargs: PS.kwargs) -> T:
        cache = LineProfilingCache.load()
        if cache.profiler is None:
            cache._debug_output(f'Calling setup hook via: {name}')
            _setup_in_child_process(cache, False, 'multiprocessing')
            assert cache.profiler is not None
        return vanilla_impl(*args, **kwargs)

    if name is None:
        name = getattr(vanilla_impl, '__name__', '???')
    return wrapper


def get_target_property() -> property:
    """
    Returns:
        Property object which wraps around the ``._target`` attribute of
        (i.e. ``target`` arguemnt to)
        :py:class:`multiprocessing.process.BaseProcess`

    Note:
        This is a hack to make sure that profiling output is written
        ASAP after the call to the target is finished.

        More intuitive solutions that just didn't work are:

        * Wrap ``target`` at initialization time:
          By replacing the callable with a wrapper, the whole process
          object becomes un-pickle-able.

        * Wrap :py:meth:`multiprocessing.process.BaseProcess._bootstrap`
          as :py:mod:`coverage` does:
          For currently unclear reasons, if the function set to
          :py:mod:`multiprocessing` raises an error, the cleanup clauses
          in a try-finally block enclosing the call to the original
          ``_bootstrap()`` implementation fails to cleanly,
          consistently, and fully execute. Something seems to be
          starting process/interpreter teardown prematurely in child
          processes...
    """
    def getter(
        self: multiprocessing.process.BaseProcess,
    ) -> Callable[..., Any] | None:
        target = vars(self).get(loc)
        if target is None:
            return None
        return cleanup_wrapper(target, name='<process target>')

    def setter(
        self: multiprocessing.process.BaseProcess,
        target: Callable[..., Any] | None,
    ) -> None:
        vars(self)[loc] = target

    def deleter(
        self: multiprocessing.process.BaseProcess,
    ) -> None:
        vars(self).pop(loc, None)

    loc = '_target'
    return property(getter, setter, deleter)


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


def log_method_call(
    self: S,
    vanilla_impl: Callable[Concatenate[S, PS], T],
    name: str,
    /,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> T:
    def get_msg(self: S, *_, **__) -> str:
        return f'Called: `.{name}()` method of {self!r}'

    return _cache_hook(
        vanilla_impl, get_msg,  # type: ignore[arg-type]
        self, *args, **kwargs,
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
        - :py:meth:`multiprocessing.process.BaseProcess._bootstrap`
          patched
        - :py:func:`multiprocessing.spawn.get_preparation_data` patched
        - Cleanup callbacks registered via `lp_cache.add_cleanup()`
    """
    if not getattr(multiprocessing, _PATCHED_MARKER, False):
        _apply_mp_patches(lp_cache, False)


def _apply_mp_patches(
    lp_cache: LineProfilingCache,
    in_child_process: bool,
    debug: bool = False,
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

    # In a child process, we don't care about polluting the
    # `multiprocessing` namespace, so don't bother with cleanup
    if in_child_process:
        add_cleanup: Callable[..., None] = _no_op
    else:
        add_cleanup = lp_cache.add_cleanup

    # Patch `multiprocessing.process.BaseProcess._bootstrap()`
    Proc = multiprocessing.process.BaseProcess
    if False:
        wrapper_maker: Callable[[Callable[PS, T]], Callable[PS, T]]
        for wrapper_maker, methods in [  # type: ignore[assignment]
            (setup_wrapper, []),
            (cleanup_wrapper, ['_bootstrap']),
        ]:
            for method in methods:
                vanilla = getattr(Proc, method)
                replace(
                    Proc, method, wrapper_maker(vanilla),
                    f'{Proc.__module__}.{Proc.__qualname__}',
                )
    else:
        # Patch `multiprocessing.process.BaseProcess._target`
        replace(
            Proc, '_target', get_target_property(),
            f'{Proc.__module__}.{Proc.__qualname__}',
        )

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
            replace(
                spawn, 'runpy', create_runpy_wrapper(lp_cache), spawn.__name__,
            )

    # Log Popen calls
    # XXX: these seem to mitigate (but not completely eliminate) the
    # issue of incomplete profiling data; the point seems to be deleying
    # the call to `Popen.terminate()` in the parent process so that
    # "bad" child processes have the chance to complete their cleanup
    # calls. Do we have a more robust way of doing this?
    if False:
        from importlib import import_module

        for submodule in [
            'popen_fork', 'popen_spawn_posix', 'popen_spawn_win32',
        ]:
            try:
                Popen = import_module('multiprocessing.' + submodule).Popen
            except ImportError:
                continue
            for method in 'kill', 'terminate', 'interrupt', 'close', 'wait':
                method_wrapper = partialmethod(
                    log_method_call, getattr(Popen, method), method,
                )
                replace(
                    Popen, method, method_wrapper,
                    f'{Popen.__module__}.{Popen.__qualname__}',
                )

    # Intercept `multiprocessing` debug messages
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
    setattr(multiprocessing, _PATCHED_MARKER, True)
    add_cleanup(vars(multiprocessing).pop, _PATCHED_MARKER, None)


def _no_op(*_, **__) -> None:
    pass
