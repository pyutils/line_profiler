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
from typing import Any, TypeVar
from typing_extensions import Concatenate, ParamSpec

from .cache import LineProfilingCache
from .pth_hook import _setup_in_child_process


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
        lp_cache._debug_output(f'cache {id(lp_cache):#x} setting up (mp)...')
        has_set_up = _setup_in_child_process(lp_cache)
        lp_cache._debug_output('cache {:#x} setup {}'.format(
            id(lp_cache), 'done' if has_set_up else 'aborted',
        ))
        # ... and we don't care about polluting the `multiprocessing`
        # namespace either, so don't bother with cleanup
        if not getattr(multiprocessing, _PATCHED_MARKER, False):
            _apply_mp_patches(lp_cache, _no_op)


def bootstrap(
    self: multiprocessing.process.BaseProcess,
    vanilla_impl: Callable[
        Concatenate[multiprocessing.process.BaseProcess, PS], T
    ],
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
        LineProfilingCache.load().cleanup()


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
    vanilla: Callable[..., Any] | None

    # Patch `multiprocessing.process.BaseProcess._bootstrap()`
    Proc = multiprocessing.process.BaseProcess
    vanilla = Proc._bootstrap  # type: ignore[attr-defined]
    Proc._bootstrap = partialmethod(  # type: ignore[attr-defined]
        bootstrap, vanilla,
    )
    add_cleanup(setattr, Proc, '_bootstrap', vanilla)

    # Patch `multiprocessing.spawn.get_preparation_data()`
    try:
        from multiprocessing import spawn
    except ImportError:  # Incompatible platforms
        pass
    else:
        vanilla = getattr(spawn, 'get_preparation_data', None)
        if vanilla:
            spawn.get_preparation_data = partial(get_preparation_data, vanilla)
            add_cleanup(setattr, spawn, 'get_preparation_data', vanilla)

    # Mark `multiprocessing` as having been patched
    setattr(multiprocessing, _PATCHED_MARKER, True)
    add_cleanup(vars(multiprocessing).pop, _PATCHED_MARKER, None)


def _no_op(*_, **__) -> None:
    pass
