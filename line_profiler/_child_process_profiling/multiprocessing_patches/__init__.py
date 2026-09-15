"""
Patch :py:mod:`multiprocessing` so that profiling extends into processes
it creates.

Notes:
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
import warnings
from collections.abc import Collection, Mapping
from functools import lru_cache
from importlib import import_module
from typing import Literal, TypeVar, cast, get_args

from ... import _diagnostics as diagnostics
from ..cache import LineProfilingCache
from .._patching_infrastructure import Registry, Patch
from .mp_config import MPConfig


__all__ = ('MPConfig', 'Registry', 'apply')

PublicPatch = Literal['pool', 'process', 'logging']
P = TypeVar('P', bound=Patch)

_PATCHED_MARKER = '__line_profiler_patched_multiprocessing__'


@lru_cache(1)
def get_registry() -> Registry:
    """
    Returns:
        :py:class:`.Registry` instance summarizing the patches loaded
        from the various ``.*_patches``  sibling modules

    Note:
        This function always return the same instance.

    Example:
        >>> reg = get_registry()
        >>> assert get_registry() is reg

        Check for the default plugins that should be installed and
        their contents:

        >>> assert 'pool' in reg
        >>> assert 'process' in reg
        >>> assert 'logging' in reg

        >>> assert (
        ...     'multiprocessing.process.BaseProcess' in reg.summary
        ... )
        >>> assert (
        ...     'worker'
        ...     in reg.summary.get('multiprocessing.pool', set())
        ... )
    """
    def check(patch: P) -> P:
        error: str | None = None
        if not hasattr(patch, 'priority'):
            error = 'expected a `.priority: float | None` field'
        elif not isinstance(getattr(patch, 'summary', None), Mapping):
            error = 'expected a `.summary: Mapping[str, Set[str]]` field'
        elif not callable(getattr(patch, 'apply', None)):
            error = (
                'expected an `.apply(cache: LineProfilingCache, ...)` '
                'method'
            )
        if error:
            raise TypeError(f'patch `{patch!r}`: {error}')
        return patch

    instance = Registry()
    subpkg = get_registry.__module__
    for name, (sibling, patch_loc) in {
        '__process_setup': ('_mandatory_patches', 'PROCESS_SETUP_PATCH'),
        '__pool_worker_pid':
            ('_mandatory_patches', 'POOL_WORKER_PID_PATCH'),
        '__resource_tracker':
            ('_mandatory_patches', 'RESOURCE_TRACKER_PATCH'),
        '__reboot_forkserver':
            ('_mandatory_patches', 'RebootForkserverPatch'),
        '__spawn_runpy': ('_mandatory_patches', 'RunpyPatch'),

        'logging': ('_optional_patches', 'LOGGING_PATCH'),

        'pool': ('_profiling_patches', 'POOL_PATCH'),
        'process': ('_profiling_patches', 'PROCESS_PATCH'),
    }.items():
        try:
            mod = import_module(f'{subpkg}.{sibling}')
            patch = check(cast(Patch, getattr(mod, patch_loc)))
        except Exception as e:
            error = type(e).__name__
            if str(error):
                error = f'{error}: {e}'
            msg = (
                f'failed to load patch {name!r} '
                f'from sibling submodule `{subpkg}.{sibling}`: {error}'
            )
            diagnostics.log.warning(msg)
            warnings.warn(msg)
        else:
            instance.register(name, patch)

    # Sanity/Consistency check
    for patch in get_args(PublicPatch):
        if patch not in instance:
            raise RuntimeError(f'Cannot load patch `{patch}`')
    return instance


def apply(
    cache: LineProfilingCache,
    reboot_forkserver: bool = True,
    patches: Collection[PublicPatch] | None = None,
) -> None:
    """
    Set up profiling in :py:mod:`multiprocessing` child processes by
    applying patches to the package.

    Args:
        cache (LineProfilingCache):
            Cache instance governing the profiling run.
        reboot_forkserver (bool):
            Whether to reboot the global
            :py:class`multiprocessing.forkserver.ForkServer` instance
            so as to ensure that profiling happens on processes forked
            therefrom (see Note).
        patches \
(Collection[Literal['pool', 'process', 'logging'] | None]):
            Patches to apply to :py:mod:`multiprocessing`; see the
            following section for a description of each;
            the default is taken from the TOML config file.

    Patches:
        ``'pool'``:
            Patch :py:class:`multiprocessing.pool.Pool` and
            :py:func:`multiprocessing.pool.worker` so that profiling
            output is recorded as each pool-worker child process
            completes a task, and written to disk as the pool is
            terminated.
        ``'process'``:
            Patch
            :py:meth:`multiprocessing.process.BaseProcess._bootstrap`
            so that non-pool-worker child processes write profiling
            output on exit.
        ``'logging'``:
            Patch :py:mod:`multiprocessing.util`'s logging methods (e.g.
            ``debug()`` and ``info()``) so that their messages are teed
            to the cache's debug log.

    Side effects:
        - The aforementioned patches applied

        - If ``reboot_forkserver=True``, fork-server process rebooted:

          - Immediately

          - When ``cache.cleanup()`` is run

        - Cleanup callbacks registered via ``cache.add_cleanup()``

    Note:
        Rebooting the fork server is necessary because its process
        statically inherits the environment when it is first spun up
        (see :py:func:`multiprocessing.forkserver.ensure_running`).
        Thus, without the reboots:

        - If in the same Python process we ever start up two separate
          profiling sessions managed by different caches, the child
          processes forked from the server will fail to inherit the
          updated environment variables injected by the newer cache
          instance, leading to the setup code in this subpackage not
          being loaded.

        - Since 3.13.8 and 3.14.1, the bug where the ``main_path``
          argument to :py:func:`multiprocessing.forkserver.main` is
          unused has been fixed (see ``cpython`` issue `GH-126631`_).
          This causes ``sys.modules['__main__']`` to be set up in the
          fork-server process, meaning that children forked therefrom
          will NOT redo the setup. Thus, the fork-server process itself
          will also need to be properly set up for profiling.

    .. _GH-126631: https://github.com/python/cpython/issues/126631
    """
    if getattr(multiprocessing, _PATCHED_MARKER, False):
        return
    if patches is None:
        patches_dict = MPConfig.from_cache(cache).patches
        patches_: set[str] = {p for p, use in patches_dict.items() if use}
    else:
        patches_ = {p.lower() for p in patches}
    for name, patch in get_registry().select(patches_).items():
        if name == '__reboot_forkserver' and not reboot_forkserver:
            continue
        msg = f'applying `multiprocessing` patch {name!r}'
        cache._debug_output(msg.capitalize() + '...')
        patch.apply(cache)
        cache._debug_output('Done with ' + msg)
    # Mark `multiprocessing` as having been patched
    cache.patch(multiprocessing, _PATCHED_MARKER, True)
