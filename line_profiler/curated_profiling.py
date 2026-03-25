"""
Tools for setting up profiling in a curated environment (e.g. with
the use of :py:mod:`kernprof`).
"""
from __future__ import annotations

import dataclasses
import os
import warnings
from collections.abc import Collection
from io import StringIO
from textwrap import indent
from typing import TextIO
from typing_extensions import Self

from . import _diagnostics as diagnostics
from .autoprofile.util_static import modpath_to_modname
from .autoprofile.eager_preimports import (
    is_dotted_path, write_eager_import_module,
)
from .cli_utils import short_string_path


__all__ = ('ClassifiedPreimportTargets',)


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
        write_module_kwargs = {
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
