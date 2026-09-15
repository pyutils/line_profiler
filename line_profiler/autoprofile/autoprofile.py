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
import os
import sys
import types
from collections.abc import Collection, MutableMapping
from typing import Any, cast

from ..toml_config import ConfigSource
from ..line_profiler_utils import restore
from .ast_tree_profiler import AstTreeProfiler, _CompoundStatement
from .run_module import AstTreeModuleProfiler
from .line_profiler_utils import (
    add_imported_function_or_module, add_star_import,
)
from .util_static import modpath_to_modname

PROFILER_LOCALS_NAME = 'prof'


def _extend_line_profiler_for_profiling_imports(prof: Any) -> None:
    """
    Allow profiler to handle imported functions/methods, classes and
    modules, and also star-import targets, with a single call. This
    adds to a :py:class:`line_profiler.LineProfiler` instance:

    - A method that can identify whether the object is a
      function/method, class, or module, and handle it's profiling
      accordingly; and

    - A method that can retrieve the names imported via a star-import
      and use the above handling to profile them.

    Mainly used for profiling objects that are imported.

    Args:
        prof (LineProfiler):
            instance of :py:class:`line_profiler.LineProfiler`.

    Notes:
        This is a workaround to keep changes needed by autoprofile
        separate from the base :py:class:`line_profiler.LineProfiler`.
    """
    for func in add_imported_function_or_module, add_star_import:
        setattr(prof, func.__name__, types.MethodType(func, prof))


def run(
    script_file: str,
    ns: MutableMapping[str, Any],
    prof_mod: list[str],
    profile_imports: bool = False,
    as_module: bool = False,
    *,
    config: os.PathLike[str] | str | None = None,
    profile_star_imports: bool | None = None,
    profile_nested_imports: Collection[_CompoundStatement] | None = None,
) -> None:
    """
    Automatically profile a script and run it, profiling functions,
    classes & modules specified in ``prof_mod`` without needing to add
    ``@profile`` decorators.

    Args:
        script_file (str):
            path to the script being profiled.

        ns (dict):
            local names to injected into the namespace where
            ``script_file``'s code is executed.

        prof_mod (List[str]):
            list of imports to profile in ``script_file``;
            passing the path ``script_file``  will profile the whole
            script via AST rewriting;
            the objects can be specified using its dotted path or
            file-system path (if applicable).

        profile_imports (bool):
            if :py:const:`True`, when rewriting the AST, profile all its
            imports aswell.

        as_module (bool):
            whether we're running ``script_file`` as a module.

        config (os.PathLike[str] | str | None):
            optional path to load the session config from.

        profile_star_imports (bool | None):
            whether to profile star-imports (``from ... import *``);
            if :py:const:`None`, the value is taken from ``config``.

        profile_nested_imports \
(Collection[Literal['func_defs', 'class_defs', \
'loops', 'conditionals', 'contexts', 'try_except']] | None):
            Which of the compound-statement types to look for nested
            imports in;
            if :py:const:`None`, it is loaded from the ``config`` (from
            ``autoprofile.import_discovery``)
    """
    Profiler: type[AstTreeModuleProfiler] | type[AstTreeProfiler]

    if as_module:
        Profiler = AstTreeModuleProfiler
        module_name = modpath_to_modname(script_file)
        if not module_name:
            raise ModuleNotFoundError(
                f'script_file = {script_file!r}: '
                'cannot find corresponding module'
            )

        module_obj = types.ModuleType(module_name)
        # Set the `__spec__` correctly
        module_obj.__spec__ = importlib.util.find_spec(module_name)
    else:
        Profiler = AstTreeProfiler
        module_obj = types.ModuleType('__main__')

    namespace: MutableMapping[str, Any] = vars(module_obj)
    namespace.update(ns)

    profiler = Profiler(
        script_file, prof_mod, profile_imports,
        config=ConfigSource.from_config(config),
    )
    tree_profiled = profiler.profile(
        profile_star_imports=profile_star_imports,
        profile_nested_imports=profile_nested_imports,
    )

    _extend_line_profiler_for_profiling_imports(ns[PROFILER_LOCALS_NAME])
    code_obj = compile(tree_profiled, script_file, 'exec')
    with restore.mapping(sys.modules, ['__main__']):
        # Always set the module object to `sys.modules['__main__']` and
        # then restore it via the context manager, so that the executed
        # code is run as `__main__`
        sys.modules['__main__'] = module_obj
        exec(
            code_obj,
            cast('dict[str, Any]', namespace),  # type: ignore[ty:redundant-cast]
            namespace,
        )
