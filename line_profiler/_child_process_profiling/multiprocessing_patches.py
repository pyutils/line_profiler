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
from functools import partial, wraps
from typing import Any, TypeVar
from typing_extensions import ParamSpec

from .cache import LineProfilingCache
from .pth_hook import _setup_in_child_process
from .runpy_patches import create_runpy_wrapper


__all__ = ('apply',)


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
        # up shop here...
        lp_cache = LineProfilingCache.load()
        _setup_in_child_process(lp_cache, False, 'multiprocessing')
        # ... and we don't care about polluting the `multiprocessing`
        # namespace either, so don't bother with cleanup
        if not getattr(multiprocessing, _PATCHED_MARKER, False):
            _apply_mp_patches(lp_cache, _no_op)


def cleanup_wrapper(vanilla_impl: Callable[PS, T]) -> Callable[PS, T]:
    """
    Wrap around :py:class:`multiprocessing.process.BaseProcess` methods
    like ``._bootstrap()``, writing the profiling results after it is
    run.

    Args:
        vanilla_impl (Callable)
            Vanilla implementation of the method

    Returns:
        Wrapper around ``vanilla_impl``

    Side effects:
        Profiling results are written as the wrapper function exits,
        before the result of ``vanilla_impl()`` is returned
    """
    @wraps(vanilla_impl)
    def wrapper(*args: PS.args, **kwargs: PS.kwargs) -> T:
        try:
            return vanilla_impl(*args, **kwargs)
        finally:  # Write profiling results
            # FIXME: somehow this finally clause is not consistently and
            # fully executed when an error occurs in the function passed
            # to `multiprocessing`... maybe the interpreter is being
            # actively exited/torned down as we speak
            cache = LineProfilingCache.load()
            cache._debug_output(f'Calling cleanup hook via: {name}')
            cache.cleanup()

    name = vanilla_impl.__name__
    return wrapper


def setup_wrapper(vanilla_impl: Callable[PS, T]) -> Callable[PS, T]:
    """
    Wrap around :py:class:`multiprocessing.process.BaseProcess` methods
    like ``.start()``, setting up profiling before it is run.

    Args:
        vanilla_impl (Callable)
            Vanilla implementation of the method

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

    name = vanilla_impl.__name__
    return wrapper


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
        _apply_mp_patches(lp_cache, lp_cache.add_cleanup)


def _apply_mp_patches(
    lp_cache: LineProfilingCache, add_cleanup: Callable[..., None],
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

    # Patch `multiprocessing.process.BaseProcess._bootstrap()`
    Proc = multiprocessing.process.BaseProcess
    for wrapper_maker, methods in [
        # (setup_wrapper, ['start']),
        (setup_wrapper, []),
        (cleanup_wrapper, ['_bootstrap']),
    ]:
        for method in methods:
            vanilla = getattr(Proc, method)
            replace(
                Proc, method, wrapper_maker(vanilla),
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

    # Mark `multiprocessing` as having been patched
    setattr(multiprocessing, _PATCHED_MARKER, True)
    add_cleanup(vars(multiprocessing).pop, _PATCHED_MARKER, None)


def _no_op(*_, **__) -> None:
    pass
