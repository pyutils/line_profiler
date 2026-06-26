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
from collections.abc import Collection
from typing import Literal, get_args

from ..cache import LineProfilingCache
from ._infrastructure import Registry
from .mp_config import MPConfig
from .poller import Poller


__all__ = ('MPConfig', 'Poller', 'Registry', 'Timeout', 'apply')

PublicPatch = Literal['pool', 'process', 'logging']

_PATCHED_MARKER = '__line_profiler_patched_multiprocessing__'
_PATCHES = Registry.from_entry_point()

Timeout = Poller.Timeout


def apply(
    cache: LineProfilingCache,
    reboot_forkserver: bool = True,
    patches: Collection[PublicPatch] | None = None,
) -> None:
    """
    Set up profiling in :py:mod:`multiprocessing` child processes by
    applying patches to the module.

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
            On Windows
                Patch :py:class:`multiprocessing.pool.Pool`'s
                ``._get_tasks()`` and ``._guarded_task_generation()``
                methods so that parallel tasks write profiling output.
            Else
                Patch :py:func:`multiprocessing.pool.worker` so that
                profiling output is written as each child process runs
                out of task.
        ``'process'``:
            Patch :py:class:`multiprocessing.process.BaseProcess`'s
            ``._bootstrap()`` method (and ``.terminate()`` on Windows)
            so that child processes write profiling output on exit and
            are given enough time for that.
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
        staticly inherits the environment when it is first spun up
        (see :py:func:`multiprocessing.forkserver.ensure_running`).
        Thus, without the reboots:

        - If in the same Python process we ever start up two separate
          profliing sessions managed by different caches, the child
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
    # Sanity check on `_PATCHES`
    for patch in get_args(PublicPatch):
        assert patch in _PATCHES
    for name, patch in _PATCHES.select(patches_).items():
        if name == '__reboot_forkserver' and not reboot_forkserver:
            continue
        msg = f'applying `multiprocessing` patch {name!r}'
        cache._debug_output(msg.capitalize() + '...')
        patch.apply(cache)
        cache._debug_output('Done with ' + msg)
    # Mark `multiprocessing` as having been patched
    cache.patch(multiprocessing, _PATCHED_MARKER, True)
