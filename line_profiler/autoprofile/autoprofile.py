import types

from line_profiler.autoprofile.ast_tree_profiler import AstTreeProfiler
from line_profiler.autoprofile.line_profiler_utils import add_imported_function_or_module


def extend_line_profiler_import_profiling(prof):
    prof.add_imported_function_or_module = types.MethodType(add_imported_function_or_module, prof)


def run(script_file, ns, prof_mod=None, profile_imports=False):
    if prof_mod is None:
        """
        what should default behaviour be if no modules to profile?
        full script profiling or no profiling?
        code copied from kernfprof
        """

        def execfile(filename, globals=None, locals=None):
            """ Python 3.x doesn't have 'execfile' builtin """
            with open(filename, 'rb') as f:
                exec(compile(f.read(), filename, 'exec'), globals, locals)

        execfile(script_file, ns, ns)
        return

    tree_profiled = AstTreeProfiler(script_file, prof_mod, profile_imports).profile()

    extend_line_profiler_import_profiling(ns['prof'])
    exec(compile(tree_profiled, script_file, 'exec'), ns, ns)
