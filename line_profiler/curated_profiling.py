"""
Tools for setting up profiling in a curated environment (e.g. with
the use of :py:mod:`kernprof`).
"""
from __future__ import annotations

import builtins
import dataclasses
import functools
import os
import warnings
from collections.abc import Callable, Collection
from io import StringIO
from textwrap import indent
from typing import Any, TextIO, cast
from typing_extensions import Self

from . import _diagnostics as diagnostics, profile as _global_profiler
from .autoprofile.util_static import modpath_to_modname
from .autoprofile.eager_preimports import (
    is_dotted_path, write_eager_import_module,
)
from .cli_utils import short_string_path
from .line_profiler import LineProfiler
from .profiler_mixin import ByCountProfilerMixin


__all__ = ('ClassifiedPreimportTargets', 'CuratedProfilerContext')


@dataclasses.dataclass
class ClassifiedPreimportTargets:
    """
    Pre-import targets classified into three bins: ``regular`` targets,
    targets to ``recurse`` into, and ``invalid`` targets
    """
    regular: list[str] = dataclasses.field(default_factory=list)
    recurse: list[str] = dataclasses.field(default_factory=list)
    invalid: list[str] = dataclasses.field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.regular or self.recurse)

    def write_preimport_module(
        self, fobj: TextIO, *, debug: bool | None = None, **kwargs
    ) -> None:
        """
        Convenience interface with
        :py:func:`~.write_eager_import_module`, writing a module which
        when imported sets up profiling of the targets.

        Args:
            fobj (TextIO):
                File object to write said module to.
            debug (Optional[bool]):
                Whether to generate debugging outputs.
            kwargs:
                Passed to :py:func:`~.write_eager_import_module`.
        """
        if self.invalid:
            invalid_targets = sorted(set(self.invalid))
            msg = (
                '{} profile-on-import target{} cannot be converted to '
                'dotted-path form: {!r}'.format(
                    len(invalid_targets),
                    '' if len(invalid_targets) == 1 else 's',
                    invalid_targets,
                )
            )
            warnings.warn(msg)
            diagnostics.log.warning(msg)

        if not self:
            return None
        # Note: `ty` (but not `mypy`) keeps complaining about the our
        # splatting this dict; explicitly use `Any` to tell it to shut
        # up.
        write_module_kwargs: dict[str, Any] = {
            'dotted_paths': self.regular,
            'recurse': self.recurse,
            **kwargs,
        }
        if diagnostics.DEBUG if debug is None else debug:
            with StringIO() as sio:
                write_eager_import_module(stream=sio, **write_module_kwargs)
                code = sio.getvalue()
            print(code, file=fobj)
            if hasattr(fobj, 'name'):
                fobj_repr = repr(short_string_path(str(fobj.name)))
            else:
                fobj_repr = repr(fobj)  # Fall back
            diagnostics.log.debug(
                f'Wrote temporary module for pre-imports to {fobj_repr}:\n'
                + indent(code, '  ')
            )
        else:
            write_eager_import_module(stream=fobj, **write_module_kwargs)

    @classmethod
    def from_targets(
        cls,
        targets: Collection[str],
        exclude: Collection[os.PathLike[str] | str] = (),
    ) -> Self:
        """
        Create an instance based on a collection of targets
        (like what is supplied to :cmd:`kernprof --prof-mod=...`).

        Args:
            targets (Collection[str])
                Collection of dotted paths and filenames to profile.
            exclude (Collection[str])
                Collections of filenames which are explicitly excluded
                from being profiled.

        Return:
            New instance.
        """
        filtered_targets = []
        recurse_targets = []
        invalid_targets = []
        for target in targets:
            if is_dotted_path(target):
                modname = target
            else:
                # Paths already normalized by
                # `_normalize_profiling_targets()`
                if not os.path.exists(target):
                    invalid_targets.append(target)
                    continue
                if any(
                    os.path.samefile(target, excluded) for excluded in exclude
                ):
                    # Ignore the script to be run in eager importing
                    # (`line_profiler.autoprofile.autoprofile.run()`
                    # will handle it)
                    continue
                modname = modpath_to_modname(target, hide_init=False)
            if modname is None:  # Not import-able
                invalid_targets.append(target)
                continue
            if modname.endswith('.__init__'):
                modname = modname.rpartition('.')[0]
                filtered_targets.append(modname)
            else:
                recurse_targets.append(modname)
        return cls(filtered_targets, recurse_targets, invalid_targets)


class CuratedProfilerContext:
    """
    Context manager for handling various bookkeeping tasks when setting
    up and tearing down profiling:

    - Slipping ``prof`` into the builtin namespace (if
      ``insert_builtin`` is true) and :py::deco:`~.profile`
    - At exit, clearing the ``enable_count`` of ``prof``, properly
      disabling it

    Note:
        The attributes on this object are to be considered
        implementation details, but not its methods and their
        signatures.
    """
    def __init__(
        self,
        prof: ByCountProfilerMixin,
        insert_builtin: bool = False,
        builtin_loc: str = 'profile',
    ) -> None:
        self.prof = prof
        self.insert_builtin = insert_builtin
        self.builtin_loc = builtin_loc
        self._installed = False
        self._kpo = _global_profiler._kernprof_overwrite

    def _global_install(self, prof: ByCountProfilerMixin | None) -> None:
        # Wrapper to convince type-checkers it is okay to pass these
        # stuff to `._kernprof_overwrite()`. We don't want to patch
        # that method's signature because passing non `LineProfiler`
        # objects to it should be the exception, not the norm.
        self._kpo(cast(LineProfiler, prof))

    def install(self) -> None:
        def del_builtin_profile() -> None:
            delattr(builtins, self.builtin_loc)

        def set_builtin_profile(old: Any) -> None:
            setattr(builtins, self.builtin_loc, old)

        if self._installed:
            return
        # Overwrite the explicit profiler (`@line_profiler.profile`)
        self._global_install(self.prof)  # type: ignore
        # Set up hooks to deal with inserting `.prof` as a builtin name
        if self.insert_builtin:
            try:
                old = getattr(builtins, self.builtin_loc)
            except AttributeError:
                self._restore: Callable[[], None] = del_builtin_profile
            else:
                self._restore = functools.partial(set_builtin_profile, old)
            set_builtin_profile(self.prof)
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        # Restore the `builtins` namespace
        if (
            self.insert_builtin
            and getattr(builtins, self.builtin_loc, None) is self.prof
        ):
            self._restore()
        # Fully disable the profiler
        for _i in range(getattr(self.prof, 'enable_count', 0)):
            self.prof.disable_by_count()
        # Restore the state of the global `@line_profiler.profile`
        self._global_install(None)
        self._installed = False

    def __enter__(self) -> None:
        self.install()

    def __exit__(self, *_, **__) -> None:
        self.uninstall()
