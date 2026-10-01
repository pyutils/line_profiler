"""
Tools for setting up profiling in a curated environment (e.g. with
the use of :py:mod:`kernprof`).

The class :py:class:`ClassifiedPreimportTargets` is responsible for
classifying profiling targets (like those supplied by
:option:`!--prof-mod`). and feeding them onwards to the machineries of
:py:mod:`line_profiler.autoprofile.eager_preimports` (see documentation
therefor), writing a script which ensures that the profiling targets are
all presented to the session's **main profiler**.

The class :py:class:`CuratedProfilerContext` is used for setting up a
profiling session, with a single associated
:py:class:`line_profiler.LineProfiler` instance as the
**main profiler**.  Said profiler is installed to various global states
as appropriate, and will be the sole instance in the process for the
collection of profiling data directly associated with the session.  The
installation will be torn worn as the session ends.

Notes:
    The intention is for there to be **at most one** profiling session,
    **one** curated context, and **one** "main profiler" at any given
    time in a single process.  Using multiple instances may result in
    undefined behavior.
"""
from __future__ import annotations

import builtins
import dataclasses
import os
import warnings
from collections.abc import Collection
from io import StringIO
from textwrap import indent
from typing import Any, TextIO
from typing_extensions import Self

from . import _diagnostics as diagnostics, profile as _GLOBAL_PROFILER
from ._threading_patches import apply as apply_threading_patches
from .autoprofile.autoprofile import (
    _extend_line_profiler_for_profiling_imports as upgrade_profiler,
)
from .autoprofile.util_static import modpath_to_modname
from .autoprofile.eager_preimports import (
    is_dotted_path, write_eager_import_module,
)
from .cleanup import Cleanup
from .cli_utils import short_string_path
from .explicit_profiler import GlobalProfiler
from .line_profiler import LineProfiler
from .profiler_mixin import ByCountProfilerMixin


__all__ = ('ClassifiedPreimportTargets', 'CuratedProfilerContext')


@dataclasses.dataclass
class ClassifiedPreimportTargets:
    """
    Pre-import targets classified into three bins: :py:attr:`.regular`
    targets, targets to :py:attr:`.recurse` into, and
    :py:attr:`.invalid` targets.
    """
    regular: list[str] = dataclasses.field(default_factory=list)
    recurse: list[str] = dataclasses.field(default_factory=list)
    invalid: list[str] = dataclasses.field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.regular or self.recurse)

    def write_preimport_module(
        self, fobj: TextIO, *, debug: bool | None = None, **kwargs
    ) -> None:
        r"""
        Convenience interface with
        :py:func:`~.write_eager_import_module`, writing a module which
        when imported sets up profiling of the targets.

        Args:
            fobj (TextIO):
                File object to write said module to.
            debug (bool | None):
                Whether to generate debugging outputs.
            kwargs:
                Passed to :py:func:`~.write_eager_import_module`.

        Example:
            >>> from pathlib import Path
            >>> from contextlib import ExitStack, redirect_stdout
            >>> from os import devnull
            >>> from tempfile import TemporaryDirectory

            >>> import pytest

            >>> with TemporaryDirectory() as tmpdir_:
            ...     tmpdir = Path(tmpdir_)
            ...     file = tmpdir / 'preimports.py'
            ...     targets = ClassifiedPreimportTargets.from_targets([
            ...         'inspect.getdoc',
            ...         str(tmpdir / 'nonexistent.py'),
            ...     ])
            ...     with ExitStack() as stack:
            ...         enter = stack.enter_context
            ...         _ = enter(pytest.warns(
            ...             match='1 .* target cannot be converted .*'
            ...             r'nonexistent\.py',
            ...         ))
            ...         fobj = enter(file.open('w'))
            ...         _ = enter(
            ...             redirect_stdout(enter(open(devnull, 'a'))),
            ...         )
            ...         targets.write_preimport_module(fobj)
            ...     line_matcher = pytest.LineMatcher(
            ...         file.read_text().splitlines(),
            ...     )

            >>> line_matcher.re_match_lines([
            ...     r'\s*import inspect', r'\s*add\(.*\bgetdoc\)',
            ... ])
        """
        if debug is None:
            debug = diagnostics.DEBUG
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
            # Log before warn in case the warning is raised
            diagnostics.log.warning(msg)
            warnings.warn(msg, stacklevel=2)

        if not self:
            return None
        # Note: `ty` (but not `mypy`) keeps complaining about our
        # splatting this dict; explicitly use `Any` to tell it to shut
        # up.
        write_module_kwargs: dict[str, Any] = {
            'dotted_paths': self.regular,
            'recurse': self.recurse,
            **kwargs,
        }
        if debug:  # nocover
            with StringIO() as sio:
                write_eager_import_module(stream=sio, **write_module_kwargs)
                code = sio.getvalue()
            print(code, end='', file=fobj)
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
        (like what is supplied to ``kernprof --prof-mod=...``).

        Args:
            targets (Collection[str]):
                Collection of dotted paths and filenames to profile.
            exclude (Collection[str | os.PathLike[str]]):
                Collections of filenames which are explicitly excluded
                from being profiled.

        Return:
            New instance.

        Example:
            >>> import multiprocessing
            >>> import os.path
            >>> import textwrap
            >>> import xml
            >>> from importlib.util import find_spec
            >>> from tempfile import TemporaryDirectory

            >>> get_targets = ClassifiedPreimportTargets.from_targets
            >>> invalid_module = 'textwrapppppp'
            >>> assert find_spec(invalid_module) is None
            >>> nonexistent_target = textwrap.__file__.replace(
            ...     'textwrap', invalid_module,
            ... )

            >>> with TemporaryDirectory() as tmpdir:
            ...     malformed_target = os.path.join(tmpdir, 'b-a-r.py')
            ...     with open(malformed_target, mode='w'):
            ...         pass  # touch
            ...     excluded_target = os.path.join(tmpdir, 'excl.py')
            ...     with open(excluded_target, mode='w'):
            ...         pass  # touch
            ...
            ...     raw_targets = [
            ...         'sys',
            ...         # Could be invalid, but we don't know ATP
            ...         'foo',
            ...         # Resolved to `'textwrap'`
            ...         textwrap.__file__,
            ...         # Invalid targets
            ...         nonexistent_target, malformed_target,
            ...         # This is valid, but will be excluded
            ...         excluded_target,
            ...         # Resolved to `'xml'` (non-recursed)
            ...         xml.__file__,
            ...         # Resolved to `'multiprocessing'` (recursed)
            ...         os.path.dirname(multiprocessing.__file__),
            ...     ]
            ...     tar = get_targets(
            ...         raw_targets, exclude=[excluded_target],
            ...     )

            >>> all_targets = set().union(tar.regular, tar.recurse)
            >>> assert {
            ...     'sys', 'foo', 'textwrap', 'multiprocessing',
            ... } <= all_targets, f'{all_targets=!r}'
            >>> assert 'xml' in tar.regular, f'{tar.regular=!r}'
            >>> assert 'excl' not in all_targets
            >>> assert {
            ...     nonexistent_target, malformed_target,
            ... } == set(tar.invalid), f'{tar=!r}; {tar.invalid=!r}'

        Notes:
            The distinction between :py:attr:`.regular` and
            :py:attr:`.recurse` is that packages in the former are
            guaranteed to NOT be recursed into, while those in the
            latter can be recursed into where appropriate (see
            :py:func:`line_profiler.autoprofile.eager_preimports.\
resolve_profiling_targets`).
            Hence, modules and packages are classified into
            :py:attr:`.recurse` by default; recursion into packages is
            stopped (i.e. target classified into :py:attr:`.regular`) by
            supplying a dotted path suffixed with ``.__init__`` or a
            file path to the package's ``__init__.py``.
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
                if not is_dotted_path(modname):
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
      ``insert_builtin`` is true) and the ``global_profiler`` instance
      (default: :py:deco:`line_profiler.profile`)

    - Patch :py:class:`threading.Thread` so that line-profiling is
      enabled on new threads if it is on the spawning threads

    - At exit, clearing the ``enable_count`` of ``prof``, properly
      disabling it

    Notes:

        - The attributes on this object are to be considered
          implementation details, but not its methods and their
          signatures.

        - This is meant to be a functional singleton; NOT MORE THAN ONE
          INSTANCE should be used in the process at any single given
          moment.  (See the module docstring.)

        - Entering the context more than once is undefined behavior.
    """
    def __init__(
        self,
        prof: ByCountProfilerMixin,
        *,
        insert_builtin: bool = False,
        builtin_loc: str = 'profile',
        global_profiler: GlobalProfiler | None = None,
    ) -> None:
        if global_profiler is None:
            global_profiler = _GLOBAL_PROFILER
        self.prof = prof
        self.insert_builtin = insert_builtin
        self.builtin_loc = builtin_loc
        self.global_profiler = global_profiler
        self._cleanup = Cleanup()
        self._installed = False

    def _global_install(self, prof: ByCountProfilerMixin | None) -> None:
        """
        Overwrite the :py:class:`line_profiler.LineProfiler` instance
        backing the :py:attr:`.global_profiler` and mark it as being
        :py:attr:`GlobalProfiler.enabled`.

        Example:
            >>> from operator import attrgetter
            >>> from line_profiler import LineProfiler
            >>> from line_profiler.explicit_profiler import (
            ...     GlobalProfiler,
            ... )

            >>> get_prof_state = attrgetter('_profile', 'enabled')
            >>> gp = GlobalProfiler()
            >>> lp = LineProfiler()
            >>> old_state = get_prof_state(gp)

            >>> with CuratedProfilerContext(lp, global_profiler=gp):
            ...     new_state = get_prof_state(gp)
            ...     assert (
            ...         old_state != new_state == (lp, True)
            ...     ), f'{new_state=!r}, {old_state=!r}'
            >>> assert (
            ...     (new_state := get_prof_state(gp))
            ...     == old_state
            ... ), f'{new_state=!r}, {old_state=!r}'

        Notes:
            Since we directly set :py:attr:`GlobalProfiler.enabled`
            instead of calling :py:meth:`GlobalProfiler.enable`, this
            doesn't register an :py:mod:`atexit` hook.  This is what we
            want because :py:mod:`kernprof` either instructs to use
            another program to read its output file or calls
            :py:meth:`line_profiler.LineStats.show` directly.
        """
        # Note: refactored from the old
        # `.GlobalProfiler._kernprof_overwrite()`.
        self._cleanup.patch(self.global_profiler, '_profile', prof)
        self._cleanup.patch(self.global_profiler, 'enabled', True)

    @staticmethod
    def _disable_profiler(prof: ByCountProfilerMixin) -> None:
        for _ in range(getattr(prof, 'enable_count', 0)):
            prof.disable_by_count()

    def _install(self) -> None:
        """
        Example:
            >>> from pytest import raises

            >>> from line_profiler import LineProfiler

            >>> class BuggedContext(CuratedProfilerContext):
            ...     '''
            ...     This class bugs out at the end of
            ...     :py:meth:`.install`, because it attempts to change
            ...     the value of :py:attr:`._installed`.
            ...     '''
            ...     @property
            ...     def _installed(self) -> bool:
            ...         return self.__installed
            ...
            ...     @_installed.setter
            ...     def _installed(self, installed: bool) -> None:
            ...         try:
            ...             self.__installed
            ...         except AttributeError:
            ...             self.__installed = installed
            ...             return
            ...         raise AttributeError('_installed')

            >>> prof = LineProfiler()

            Normal execution:

            >>> with CuratedProfilerContext(
            ...     prof, insert_builtin=True, builtin_loc='foo',
            ... ):
            ...     assert foo is prof  # `foo` inserted above
            >>> with raises(NameError):
            ...     assert foo is not prof  # `foo` reverted

            Botched installation:

            >>> # Context managed
            >>> with raises(AttributeError, match='_installed'):
            ...     with BuggedContext(
            ...         prof, insert_builtin=True, builtin_loc='foo',
            ...     ):
            ...         raise RuntimeError  # Unreachable (setup failed)
            ... with raises(NameError):
            ...     assert foo is not prof  # `foo` reverted

            >>> # Explicit invocation
            >>> ctx = BuggedContext(
            ...     prof, insert_builtin=True, builtin_loc='foo',
            ... )
            >>> with raises(AttributeError, match='_installed'):
            ...     ctx.install()
            >>> with raises(NameError):
            ...     assert foo is not prof  # `foo` reverted
        """
        if self._installed:
            return
        cleanup = self._cleanup
        # Equip the profiler instance with the
        # `.add_imported_function_or_module()` pseudo-method
        upgrade_profiler(self.prof, cleanup=cleanup)
        # Overwrite the explicit profiler (`@line_profiler.profile`)
        self._global_install(self.prof)
        # Patch `threading`
        if isinstance(self.prof, LineProfiler):
            apply_threading_patches(cleanup, self.prof)
        # Set up hooks to deal with inserting `.prof` as a builtin name
        if self.insert_builtin:
            cleanup.patch(builtins, self.builtin_loc, self.prof)
        # Disable the profiler at session exit
        cleanup.add_cleanup(self._disable_profiler, self.prof)
        # Indicate that we shouldn't redo the installation as a failsafe
        cleanup.patch(self, '_installed', True)

    def install(self) -> None:
        """
        Perform setup (see the class docstring).
        """
        try:
            self._install()
        except BaseException as e:
            # If anything goes south, immediately roll back all the
            # installed changes
            xc = type(e).__name__
            if (detail := str(xc)):
                xc = f'{xc}: {detail}'
            try:  # This shouldn't raise, but just in case...
                self._cleanup.cleanup(reason=f'installation failed ({xc})')
            finally:
                raise e

    def uninstall(self) -> None:
        """
        Tear down all the setup.
        """
        self._cleanup.cleanup(reason='uninstalling profiling context')

    def __enter__(self) -> Self:
        self.install()
        return self

    def __exit__(self, *_, **__) -> None:
        self.uninstall()
