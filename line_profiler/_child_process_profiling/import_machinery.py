"""
A meta path finder object which rewrites a specific module.

Note:
    Based on the implementation in
    :py:mod:`pytest_autoprofile.importers`.
"""
from __future__ import annotations

import ast
import os
import sys
from collections.abc import Callable
from functools import partial
from importlib.abc import MetaPathFinder, SourceLoader
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import CodeType, ModuleType
from typing import TYPE_CHECKING

from ..autoprofile.run_module import AstTreeModuleProfiler
from ..line_profiler import LineProfiler
from .cache import LineProfilingCache


__all__ = ('RewritingFinder',)


def _check_module_name(name: str, spec: ModuleSpec) -> bool:
    return spec.name == name


def _check_module_origin(
    path: os.PathLike[str] | str, spec: ModuleSpec,
) -> bool:
    if spec.origin is None:
        return False
    return os.path.samefile(path, spec.origin)


class RewritingFinder(MetaPathFinder, SourceLoader):
    """
    Meta path finder to be set up in child processes, so that the
    ``module_to_rewrite`` is rewritten for profiling as
    :py:func:`line_profiler.autoprofile.autoprofile.run` does.
    """
    _cached_code_obj: CodeType
    _checks: list[Callable[[ModuleSpec], bool]]

    def __init__(
        self,
        prof: LineProfiler,
        lp_cache: LineProfilingCache,
        module_to_rewrite: str = '__main__',
    ) -> None:
        self.prof = prof
        self.lp_cache = lp_cache
        self._checks = []
        self._checks.append(partial(_check_module_name, module_to_rewrite))
        if lp_cache.rewrite_module:
            self._checks.append(
                partial(_check_module_origin, lp_cache.rewrite_module)
            )

    def install(self, *, index: int = 0) -> None:
        """
        Install the importer into :py:data:`sys.meta_path` at the
        specified ``index``;
        if it's already there, it's first removed and re-inserted at
        the requested position.
        """
        self.uninstall(invalidate_caches=False)
        sys.meta_path.insert(index, self)

    def uninstall(self, *, invalidate_caches: bool = True) -> None:
        """
        Uninstall the importer from :py:data:`sys.meta_path`, and
        optionally also invalidate the caches.
        """
        try:
            sys.meta_path.remove(self)
        except ValueError:  # Not in the list
            return
        if invalidate_caches:
            self.invalidate_caches()

    @classmethod
    def find_spec_by_path(cls, *args, **kwargs) -> ModuleSpec | None:
        """
        Implementation of
        :py:meth:`MetaPathFinder.find_spec` which looks
        for module specs with the other meta-path finders.

        Returns:
            maybe_spec (ModuleSpec | None)
                Module spec if found, :py:const:`None` otherwise
        """
        Implementation = Callable[..., ModuleSpec | None]
        impls: list[Implementation] = [
            finder.find_spec for finder in sys.meta_path
            if callable(getattr(finder, 'find_spec', None))
            if not isinstance(finder, cls)
        ]
        for impl in impls:
            try:
                spec = impl(*args, **kwargs)
            except Exception:
                continue
            if spec is not None:
                return spec
        return None

    def write_code(self, spec: ModuleSpec) -> CodeType:
        """
        Rewrite the module code that ``spec`` points to with
        :py:class:`ast.autoprofile.run_module.AstTreeModuleProfiler`.

        Args:
            spec (ModuleSpec)
                Module spec

        Returns:
            code (CodeType)
                Code object
        """
        assert spec.origin
        fname = str(Path(spec.origin))
        module = AstTreeModuleProfiler(
            spec.origin,
            list(self.lp_cache.profiling_targets),
            self.lp_cache.profile_imports,
        ).profile()
        # Slip in a helper node so as to ensure the availability of
        # `@profile`
        # Note: `@profile` is slipped into the local namespace by
        # `RewritingFinder.exec_module()`, but that may not be
        # enough for applications directly using the code objects (e.g.
        # `runpy`. Hence, we provide an out by falling back to
        # `@line_profiler.profile`.
        ensure_profile_node, = ast.parse(
            'if profile not in globals():\n'
            '    from line_profiler import profile'
        ).body
        module.body.insert(0, ensure_profile_node)
        return compile(module, fname, 'exec')

    # Methods as dictated by the interface

    def invalidate_caches(self) -> None:
        try:
            del self._cached_code_obj
        except AttributeError:
            pass
        super().invalidate_caches()

    def exec_module(self, module: ModuleType) -> None:
        namespace = module.__dict__
        namespace['profile'] = prof = self.prof
        spec: ModuleSpec | None = module.__spec__
        if spec is None:
            raise RuntimeError(f'module = {module!r}: empty `.__spec__`')
        if TYPE_CHECKING:  # Appease `mypy`
            assert hasattr(prof, 'object_count')
        count = prof.object_count
        exec(self.get_code(spec), namespace, namespace)
        if prof.object_count > count:
            msg = '{}: profiled {} code object{} in the `{}` module'.format(
                type(self).__name__,
                prof.object_count,
                '' if prof.object_count == 1 else 's',
                spec.name,
            )
            self.lp_cache._debug_output(msg)

    def find_spec(self, *args, **kwargs) -> ModuleSpec | None:
        spec = self.find_spec_by_path(*args, **kwargs)
        if spec is None:
            return None
        for check in self._checks:
            try:
                if check(spec):
                    spec.loader = self
                    return spec
            except Exception:
                pass
        return None

    @staticmethod
    def get_data(path: os.PathLike[str] | str) -> bytes:
        return Path(path).read_bytes()

    @classmethod
    def get_filename(cls, name: str) -> str:
        spec = cls.find_spec_by_path(name)
        if spec is None:
            raise ImportError(name)
        origin = spec.origin
        if origin is None:
            raise ImportError(name)
        if origin == 'frozen' or not os.path.exists(origin):
            raise ImportError(name)
        return origin

    def get_code(self, name_or_spec: str | ModuleSpec) -> CodeType:
        if isinstance(name_or_spec, str):
            spec = self.find_spec_by_path(name_or_spec)
            if spec is None:
                raise ImportError(name_or_spec)
        else:
            spec = name_or_spec
        try:
            try:
                return self._cached_code_obj
            except AttributeError:
                self._cached_code_obj = code = self.write_code(spec)
                return code
        except Exception as e:
            raise ImportError(name_or_spec) from e

    if TYPE_CHECKING:
        def source_to_code(  # type: ignore[override]
            self, *args, **kwargs
        ) -> CodeType:
            """
            Notes
            -----
            :py:mod:`mypy` reports that
            :py:meth:`SourceLoader.source_to_code`, an instance method,
            clashes with
            :py:meth:`importlib.abc.InspectLoader.source_to_code`, a
            static method.

            Since:
            - The method is functionally only used in the superclasses
              as an instance method, and
            - :py:class:`importlib.abc.InspectLoader`` is merely
              included as a base class because :py:class:`SourceLoader`
              inherits from it,
            just explicitly override the method here so that we can
            catch and suppress the :py:mod:`mypy` error.
            """
            return super().source_to_code(*args, **kwargs)
