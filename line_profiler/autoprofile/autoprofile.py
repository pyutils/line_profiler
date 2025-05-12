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
import contextlib
import functools
import importlib.util
import operator
import sys
import types
from .ast_tree_profiler import AstTreeProfiler
from .run_module import AstTreeModuleProfiler
from .line_profiler_utils import add_imported_function_or_module
from .util_static import modpath_to_modname

PROFILER_LOCALS_NAME = 'prof'


def _extend_line_profiler_for_profiling_imports(prof):
    """Allow profiler to handle functions/methods, classes & modules with a single call.

    Add a method to LineProfiler that can identify whether the object is a
    function/method, class or module and handle it's profiling accordingly.
    Mainly used for profiling objects that are imported.
    (Workaround to keep changes needed by autoprofile separate from base LineProfiler)

    Args:
        prof (LineProfiler):
            instance of LineProfiler.
    """
    prof.add_imported_function_or_module = types.MethodType(add_imported_function_or_module, prof)


def run(script_file, ns, prof_mod, profile_imports=False, as_module=False):
    """Automatically profile a script and run it.

    Profile functions, classes & modules specified in prof_mod without needing to add
    @profile decorators.

    Args:
        script_file (str):
            path to script being profiled.

        ns (dict):
            "locals" from kernprof scope.

        prof_mod (List[str]):
            list of imports to profile in script.
            passing the path to script will profile the whole script.
            the objects can be specified using its dotted path or full path (if applicable).

        profile_imports (bool):
            if True, when auto-profiling whole script, profile all imports aswell.

        as_module (bool):
            Whether we're running script_file as a module
    """
    class restore_dict:
        def __init__(self, d, target=None):
            self.d = d
            self.target = target
            self.copy = None

        def __enter__(self):
            assert self.copy is None
            self.copy = self.d.copy()
            return self.target

        def __exit__(self, *_, **__):
            self.d.clear()
            self.d.update(self.copy)
            self.copy = None

    if as_module:
        Profiler = AstTreeModuleProfiler
        module_name = modpath_to_modname(script_file)
        if not module_name:
            raise ModuleNotFoundError(f'script_file = {script_file!r}: '
                                      'cannot find corresponding module')

        module_obj = types.ModuleType(module_name)
        namespace = vars(module_obj)
        namespace.update(ns)

        # Set the `__spec__` correctly
        module_obj.__spec__ = importlib.util.find_spec(module_name)

        # Set the module object to `sys.modules` via a callback, and
        # then restore it via the context manager
        callback = functools.partial(
            operator.setitem, sys.modules, '__main__', module_obj)
        ctx = restore_dict(sys.modules, callback)
    else:
        Profiler = AstTreeProfiler
        namespace = ns
        ctx = contextlib.nullcontext(lambda: None)

    profiler = Profiler(script_file, prof_mod, profile_imports)
    tree_profiled = profiler.profile()

    _extend_line_profiler_for_profiling_imports(ns[PROFILER_LOCALS_NAME])
    code_obj = compile(tree_profiled, script_file, 'exec')
    with ctx as callback:
        callback()
        exec(code_obj, namespace, namespace)
