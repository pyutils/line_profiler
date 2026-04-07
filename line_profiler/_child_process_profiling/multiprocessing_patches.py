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
from functools import partial, partialmethod
from typing import Any, TypedDict, TypeVar
from typing_extensions import Concatenate, ParamSpec

from .cache import LineProfilingCache
from .pth_hook import _setup_in_child_process


__all__ = ('apply',)


T = TypeVar('T')
PS = ParamSpec('PS')


class _HookState(TypedDict):
    cache_path: str  # Cache to be loaded from here


class PickleHook:
    """
    Object which, when unpickled, sets up profiling in the
    :py:mod:`multiprocessing`-created process.

    See also:
        :py:class:`coverage.multiproc.Stowaway`
    """
    def __init__(self, cache_path: str) -> None:
        self.cache_path = cache_path

    def __getstate__(self) -> _HookState:
        return {'cache_path': self.cache_path}

    def __setstate__(self, state: _HookState) -> None:
        self.cache_path = path = state['cache_path']
        apply(path)


def bootstrap(
    self: multiprocessing.process.BaseProcess,
    vanilla_impl: Callable[
        Concatenate[multiprocessing.process.BaseProcess, PS], T
    ],
    lp_cache: LineProfilingCache,
    /,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> T:
    """
    Wrap around
    :py:meth:`multiprocessing.process.BaseProcess._bootstrap`,
    writing the profiling results after it is run.

    Args:
        self (multiprocessing.process.BaseProcess)
            :py:class:`~.BaseProcess`
        vanilla_impl (Callable)
            Vanilla :py:meth:`~.BaseProcess._bootstrap`
        lp_cache (LineProfilingCache)
            Cache recovered by :py:meth:`~.LineProfilingCache.load`
        *args
        **kwargs
            Passed to :py:meth:`~.BaseProcess._bootstrap`

    Returns:
        Return value of ``vanilla_impl(*args, **kwargs)``

    Side effects:
        Profiling results are written
    """
    try:
        return vanilla_impl(self, *args, **kwargs)
    finally:  # Write profiling results
        lp_cache.cleanup()


def get_preparation_data(
    vanilla_impl: Callable[PS, dict[str, Any]],
    cache_path: str,
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
        cache_path
            File from which the :py:class:`LineProfilingCache` should be
            loaded
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
    data[key] = PickleHook(cache_path)
    return data


def apply(
    cache_path: str, *, lp_cache: LineProfilingCache | None = None,
) -> None:
    """
    Set up profiling in :py:mod:`multiprocessing` child processes by
    applying patches to the module.

    Args:
        cache_path
            Path to the file whence a :py:class:`LineProfilingCache`
            object can be loaded
        lp_cache
            Optional :py:class:`LineProfilingCache` instance;
            if not provided, it is loaded from `cache_path`, and
            profiling is set up therefrom in the (sub-)process

    Side effects:
        - :py:mod:`multiprocessing` marked as having been set up
        - :py:meth:`multiprocessing.process.BaseProcess._bootstrap`
          patched
        - :py:func:`multiprocessing.spawn.get_preparation_data` patched
        - Cleanup callbacks registered via `lp_cache.add_cleanup()`
    """
    patched_marker = '_line_profiler_patched_multiprocessing'
    if getattr(multiprocessing, patched_marker, False):
        return
    if lp_cache is None:
        lp_cache = LineProfilingCache._from_path(cache_path)
        # Hack to retrieve the `.load()`-ed instance if one exists
        loaded_cache = LineProfilingCache._loaded_instance
        if (
            loaded_cache is not None
            and loaded_cache._get_init_args() == lp_cache._get_init_args()
        ):
            lp_cache = loaded_cache
        lp_cache._debug_output(f'cache {id(lp_cache):#x} setting up (mp)...')
        has_set_up = _setup_in_child_process(lp_cache)
        lp_cache._debug_output('cache {:#x} setup {}'.format(
            id(lp_cache), 'done' if has_set_up else 'aborted',
        ))

    vanilla: Callable[..., Any] | None

    # Patch `multiprocessing.process.BaseProcess._bootstrap()`
    Proc = multiprocessing.process.BaseProcess
    vanilla = Proc._bootstrap  # type: ignore[attr-defined]
    Proc._bootstrap = (  # type: ignore[attr-defined]
        partialmethod(bootstrap, vanilla, lp_cache)
    )
    lp_cache.add_cleanup(setattr, Proc, '_bootstrap', vanilla)

    # Patch `multiprocessing.spawn.get_preparation_data()`
    try:
        from multiprocessing import spawn
    except ImportError:  # Incompatible platforms
        pass
    else:
        vanilla = getattr(spawn, 'get_preparation_data', None)
        if vanilla:
            spawn.get_preparation_data = partial(
                get_preparation_data, vanilla, cache_path,
            )
            lp_cache.add_cleanup(
                setattr, spawn, 'get_preparation_data', vanilla,
            )

    # Mark `multiprocessing` as having been patched
    setattr(multiprocessing, patched_marker, True)
    lp_cache.add_cleanup(vars(multiprocessing).pop, patched_marker, None)
