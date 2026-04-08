"""
Patches for :py:mod:`runpy` to be patched into the namespace of
:py:mod:`multiprocessing.spawn`, so that the rewriting of ``__main__``
can be continued into child processes.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from functools import partial
from importlib.util import find_spec
from types import ModuleType
from typing import cast, TypeVar
from typing_extensions import Concatenate, ParamSpec

from ..autoprofile.ast_tree_profiler import AstTreeProfiler
from ..autoprofile.run_module import AstTreeModuleProfiler
from ..autoprofile.util_static import modname_to_modpath
from .cache import LineProfilingCache


__all__ = ('create_runpy_wrapper',)


PS = ParamSpec('PS')
T = TypeVar('T')


THIS_MODULE = (lambda: None).__module__


def _copy_module(name: str) -> ModuleType:
    """
    Returns:
        module (ModuleType):
            Module object, which is a fresh copy of the module named
            ``name``
    """
    spec = find_spec(name)
    if spec is None:
        raise ModuleNotFoundError(name)
    assert spec.loader
    assert callable(getattr(spec.loader, 'exec_module', None))
    module = ModuleType(spec.name)
    for attr, value in {
        '__spec__': spec,
        '__name__': spec.name,
        '__file__': spec.origin,
        '__path__': spec.submodule_search_locations,
    }.items():
        if value is not None:
            setattr(module, attr, value)
    spec.loader.exec_module(module)
    return module


def _exec(
    cache: LineProfilingCache,
    CodeWriter: type[AstTreeProfiler],
    _code,  # This represents the first pos arg to `exec()` (ignored)
    /,
    *args, **kwargs,
) -> None:
    """
    To be monkey-patched into :py:mod:`runpy`'s namespace as `exec()`
    so that rewritten and autoprofiled code at ``cache.rewrite_module``
    is always executed.
    """
    assert cache.rewrite_module
    cache._debug_output('Calling via {}: `exec({})`'.format(
        THIS_MODULE,
        ', '.join(
            [repr(a) for a in (_code, *args)]
            + [f'{k}={v!r}' for k, v in kwargs.items()]
        ),
    ))
    fname = str(cache.rewrite_module)
    code_writer = CodeWriter(
        fname,
        list(cache.profiling_targets),
        cache.profile_imports,
    )
    code = compile(code_writer.profile(), fname, 'exec')
    exec(code, *args, **kwargs)


def _run(
    cache: LineProfilingCache,
    runpy: ModuleType,
    func: Callable[Concatenate[str, PS], T],
    resolve_target_to_path: Callable[[str], str],
    CodeWriter: type[AstTreeProfiler],
    target: str,
    /,
    *args: PS.args, **kwargs: PS.kwargs
) -> T:
    cache._debug_output('Calling via {}: `runpy.{}({})`'.format(
        THIS_MODULE,
        func.__name__,
        ', '.join(
            [repr(a) for a in (target, *args)]
            + [f'{k}={v!r}' for k, v in kwargs.items()]
        ),
    ))
    if cache.rewrite_module:
        try:
            filename = resolve_target_to_path(target)
            profile = os.path.samefile(filename, cache.rewrite_module)
        except Exception as e:
            cache._debug_output(
                f'{THIS_MODULE}: failed to check whether to '
                f'rewrite code loaded in `runpy.{func.__name__}(...)` '
                f'({type(e).__name__}: {e})'
            )
            profile = False
    else:
        profile = False
    # If we are about to run the code to be autoprofiled, monkey-patch
    # `exec()` into the `runpy` namespace which just rewrites
    # `cache.rewrite_module` and executes it
    if profile:
        runpy.exec = (  # type: ignore[attr-defined]
            partial(_exec, cache, CodeWriter)
        )
    try:
        return func(target, *args, **kwargs)
    finally:
        try:
            del runpy.exec
        except AttributeError:
            pass


def create_runpy_wrapper(cache: LineProfilingCache) -> ModuleType:
    """
    Create a copy of :py:mod:`runpy` which does code rewriting similar
    to :py:func:`line_profiler.autoprofile.autoprofile.run` for the
    appropriate file as indicated by ``cache``.
    """
    runpy = _copy_module('runpy')
    for func, resolver, CodeWriter in [
        ('run_path', str, AstTreeProfiler),
        ('run_module', modname_to_modpath, AstTreeModuleProfiler),
    ]:
        impl = getattr(runpy, func)
        res = cast(Callable[[str], str], resolver)  # Help `mypy` out
        wrapper = partial(_run, cache, runpy, impl, res, CodeWriter)
        setattr(runpy, func, wrapper)
    return runpy
