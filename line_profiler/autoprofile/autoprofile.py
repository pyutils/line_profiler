import os
import types

from line_profiler.autoprofile.ast_profiler import get_ast_tree,profile_ast_tree
from line_profiler.autoprofile.line_profiler_utils import add_imported_function_or_module
from line_profiler.autoprofile.profmod_extractor import ProfmodExtractor


def extend_line_profiler_import_profiling(prof):
    prof.add_imported_function_or_module = types.MethodType(add_imported_function_or_module, prof)


def check_prof_mod_profile_full_script(script_file, prof_mod):
    script_file_realpath = os.path.realpath(script_file)
    for mod in prof_mod:
        if os.path.realpath(mod) == script_file_realpath:
            return True
    return False


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


    tree = get_ast_tree(script_file)

    names_to_profile, chosen_indexes, modules_index = ProfmodExtractor().run(script_file, prof_mod, tree)

    profile_script = check_prof_mod_profile_full_script(script_file, prof_mod)

    tree_profiled = profile_ast_tree(tree, names_to_profile, chosen_indexes, modules_index,
                                     profile_script=profile_script,
                                     profile_script_imports=profile_imports)

    extend_line_profiler_import_profiling(ns['prof'])
    exec(compile(tree_profiled, script_file, 'exec'), ns, ns)
