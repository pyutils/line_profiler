"""

AutoProfile Script Demo
=======================

The following demo is end-to-end bash code that writes a demo script and
profiles it with autoprofile.

.. code:: bash

    # Write demo python script to disk
    python -c "if 1:
        import textwrap
        text = textwrap.dedent(
            '''
            def plus(a, b):
                return a + b

            def fib(n):
                a, b = 0, 1
                while a < n:
                    a, b = b, plus(a, b)

            def main():
                import math
                import time
                start = time.time()

                print('start calculating')
                while time.time() - start < 1:
                    fib(10)
                    math.factorial(1000)
                print('done calculating')

            main()
            '''
        ).strip()
        with open('demo.py', 'w') as file:
            file.write(text)
    "

    echo "---"
    echo "## Profile With AutoProfile"
    python -m kernprof -p demo.py -l demo.py
    python -m line_profiler -rmt demo.py.lprof
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Callable, Mapping, MutableMapping
from functools import partial
from typing import Any, cast

from ..cleanup import Cleanup
from ..line_profiler_utils import restore
from .ast_tree_profiler import AstTreeProfiler
from .run_module import AstTreeModuleProfiler
from .line_profiler_utils import add_imported_function_or_module
from .util_static import modpath_to_modname


PROFILER_LOCALS_NAME = 'prof'

_EXTENSION_METHODS: dict[str, Callable[..., Any]] = {
    'add_imported_function_or_module': add_imported_function_or_module,
}


def _extend_line_profiler_for_profiling_imports(
    prof: Any,
    cleanup: Cleanup | None = None,
    methods: Mapping[str, Callable[..., Any]] | None = None,
    **kwargs
) -> None:
    """
    Extend ``prof`` to handle functions/methods, classes & modules with
    a single call.

    Equip the :py:class:`LineProfiler` instance with pseudo-methods that
    can identify whether the object is a function/method, class or
    module and handle it's profiling accordingly.  Mainly used for
    profiling objects that are imported in a
    :py:mod:`line_profiler.curated_profiling` context.

    Args:
        prof (LineProfiler):
            Profiler instance.
        cleanup (Cleanup | None):
            Optional :py:class:`Cleanup` object used for managing the
            equipping of the methods; if not provided, they are just
            bolted on with :py:func:`setattr`.
        methods (Mapping[str, Callable[..., Any]] | None):
            Optional mapping from pseudo-method names to the
            instance-method implementation callables; if not provided,
            default to the tools defined in
            :py:mod:`line_profiler.autoprofile.line_profiler_utils`.
        **kwargs:
            Passed to :py:meth:`Cleanup.patch`.

    Notes:
        This is a workaround to keep changes needed by autoprofile
        separate from the base :py:class:`LineProfiler`.

    Example:
        >>> from functools import partial
        >>> from typing import cast

        >>> from line_profiler import LineProfiler
        >>> from line_profiler.cleanup import Cleanup

        >>> class MockProfiler:
        ...     pass

        >>> def test(prof: MockProfiler, *args, **kwargs) -> None:
        ...     lprof = cast(LineProfiler, prof)
        ...     assert not hasattr(
        ...         lprof, 'get_id',
        ...     ), f'{lprof.get_id=!r}'
        ...     assert not hasattr(
        ...         lprof, 'double',
        ...     ), f'{lprof.double=!r}'
        ...
        ...     extend(lprof, *args, **kwargs)
        ...     assert (res := lprof.get_id()) == id(lprof), f'{res=!r}'
        ...     assert (res := lprof.double(5)) == 10, f'{res=!r}'

        >>> extensions = {
        ...     'get_id': lambda self: id(self),
        ...     'double': lambda _, x: 2 * x
        ... }
        >>> extend = partial(
        ...     _extend_line_profiler_for_profiling_imports,
        ...     methods=extensions,
        ... )

        Normal use:

        >>> test(MockProfiler())

        Managed use (w/``cleanup``):

        >>> prof = MockProfiler()
        >>> with Cleanup() as cleanup:
        ...     test(prof, cleanup=cleanup)
        ...     assert hasattr(prof, 'get_id')
        ...     assert hasattr(prof, 'double')
        >>> # The equipped methods should be restored outside the
        >>> # context
        >>> assert not hasattr(prof, 'get_id'), f'{prof.get_id=!r}'
        >>> assert not hasattr(prof, 'double'), f'{prof.double=!r}'
    """
    if cleanup is None:
        set_attr: Callable[[Any, str, Any], None] = setattr
    else:
        set_attr = partial(cleanup.patch, **kwargs)
    if methods is None:
        methods = _EXTENSION_METHODS
    for name, impl in methods.items():
        set_attr(prof, name, types.MethodType(impl, prof))


def run(
    script_file: str,
    ns: MutableMapping[str, Any],
    prof_mod: list[str],
    profile_imports: bool = False,
    as_module: bool = False,
) -> None:
    """Automatically profile a script and run it.

    Profile functions, classes & modules specified in ``prof_mod``
    without needing to add ``@profile`` decorators.

    Args:
        script_file (str):
            path to script being profiled.

        ns (MutableMapping[str, Any]):
            "locals" from kernprof scope.

        prof_mod (list[str]):
            list of imports to profile in script.
            passing the path to script will profile the whole script.
            the objects can be specified using its dotted path or full
            path (if applicable).

        profile_imports (bool):
            if :py:const:`True`, when auto-profiling whole script,
            profile all imports aswell.

        as_module (bool):
            Whether we're running ``script_file`` as a module
    """
    Profiler: type[AstTreeModuleProfiler] | type[AstTreeProfiler]

    if as_module:
        Profiler = AstTreeModuleProfiler
        module_name = modpath_to_modname(script_file)
        if not module_name:
            raise ModuleNotFoundError(
                f'script_file = {script_file!r}: '
                'cannot find corresponding module',
            )

        module_obj = types.ModuleType(module_name)
        # Set the `__spec__` correctly
        module_obj.__spec__ = importlib.util.find_spec(module_name)
    else:
        Profiler = AstTreeProfiler
        module_obj = types.ModuleType('__main__')

    namespace: MutableMapping[str, Any] = vars(module_obj)
    namespace.update(ns)

    profiler = Profiler(script_file, prof_mod, profile_imports)
    tree_profiled = profiler.profile()

    _extend_line_profiler_for_profiling_imports(ns[PROFILER_LOCALS_NAME])
    code_obj = compile(tree_profiled, script_file, 'exec')
    with restore.mapping(sys.modules, ['__main__']):
        # Always set the module object to `sys.modules['__main__']` and
        # then restore it via the context manager, so that the executed
        # code is run as `__main__`
        sys.modules['__main__'] = module_obj
        exec(code_obj, cast('dict[str, Any]', namespace), namespace)
