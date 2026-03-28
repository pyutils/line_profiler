"""
A cache object to be used by for propagating profiling down to child
processes.
"""
from __future__ import annotations

import dataclasses
import os
try:
    import _pickle as pickle
except ImportError:
    import pickle  # type: ignore[assignment,no-redef]
from collections.abc import Collection, Callable
from functools import partial
from pickle import HIGHEST_PROTOCOL
from typing import Any
from typing_extensions import Self, ParamSpec

# Note: this should have been defined here in this file, but we moved it
# over to `~._child_process_hook` because that module contains the .pth
# hook, which must run with minimal overhead when a Python process isn't
# associated with a profiled process
from .pth_hook import INHERITED_PID_ENV_VARNAME


__all__ = ('LineProfilingCache',)


PS = ParamSpec('PS')

INHERITED_CACHE_ENV_VARNAME_PREFIX = (
    'LINE_PROFILER_PROFILE_CHILD_PROCESSES_CACHE_DIR'
)
CACHE_FILENAME = 'line_profiler_cache.pkl'


@dataclasses.dataclass
class LineProfilingCache:
    cache_dir: os.PathLike[str] | str
    profiling_targets: Collection[str] = dataclasses.field(
        default_factory=list,
    )
    preimports_module: os.PathLike[str] | str | None = None
    main_pid: int = dataclasses.field(default_factory=os.getpid)
    insert_builtin: bool = True
    _cleanup_stack: list[Callable[[], Any]] = dataclasses.field(
        default_factory=list, init=False,
    )

    def cleanup(self) -> None:
        """
        Pop all the cleanup callbacks from the internal stack added via
        :py:meth:`~.add_cleanup` and call them in order.
        """
        callbacks = self._cleanup_stack
        while callbacks:
            callback = callbacks.pop()
            try:
                callback()
            except Exception:
                pass

    def add_cleanup(
        self, callback: Callable[PS, Any], *args: PS.args, **kwargs: PS.kwargs,
    ) -> None:
        """
        Add a cleanup callback to the internal stack; they can be later
        called by :py:meth:`~.cleanup`.
        """
        if args or kwargs:
            callback = partial(callback, *args, **kwargs)
        self._cleanup_stack.append(callback)

    def copy(
        self, *, inherit_cleanups: bool = False, **replacements
    ) -> Self:
        """
        Make a copy with optionally replaced fields;
        if ``inherit_cleanups`` is set to true, the copy also makes a
        (shallow) copy of the clean-callback stack.
        """
        init_args: dict[str, Any] = {}
        for field, value in self._get_init_args().items():
            init_args[field] = replacements.get(field, value)
        copy = type(self)(**init_args)
        if inherit_cleanups:
            copy._cleanup_stack[:] = self._cleanup_stack
        return copy

    @classmethod
    def load(cls) -> Self:
        """
        Reconstruct the instance from the environment variables
        :env:`LINE_PROFILER_PROFILE_CHILD_PROCESSES_CACHE_PID` and
        :env:`LINE_PROFILER_PROFILE_CHILD_PROCESSES_CACHE_DIR_<PID>`.
        These should have been set from an ancestral Python process.
        """
        pid = os.environ[INHERITED_PID_ENV_VARNAME]
        cache_dir = os.environ[f'{INHERITED_CACHE_ENV_VARNAME_PREFIX}_{pid}']
        with open(cls._get_filename(cache_dir), mode='rb') as fobj:
            return cls(**pickle.load(fobj))

    def dump(self) -> None:
        """
        Serialize the cache instance and dump into the default location
        as indicated by :py:attr:`~.cache_dir`, so that they can be
        :py:meth:`~.load`-ed by child processes.

        Note:
            Cleanup callbacks are not serialized.
        """
        with open(self.filename, mode='wb') as fobj:
            pickle.dump(
                self._get_init_args(), fobj, protocol=HIGHEST_PROTOCOL,
            )

    def _get_init_args(self) -> dict[str, Any]:
        init_fields = [
            field_obj.name for field_obj in dataclasses.fields(self)
            if field_obj.init
        ]
        return {name: getattr(self, name) for name in init_fields}

    @staticmethod
    def _get_filename(cache_dir: os.PathLike[str] | str) -> str:
        return os.path.join(cache_dir, CACHE_FILENAME)

    @property
    def environ(self) -> dict[str, str]:
        """
        Environment variables to be injected into and inherited by child
        processes.
        """
        cache_varname = f'{INHERITED_CACHE_ENV_VARNAME_PREFIX}_{self.main_pid}'
        return {
            INHERITED_PID_ENV_VARNAME: str(self.main_pid),
            cache_varname: str(self.cache_dir),
        }

    @property
    def filename(self) -> str:
        return self._get_filename(self.cache_dir)
