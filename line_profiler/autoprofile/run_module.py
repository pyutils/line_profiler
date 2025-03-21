import ast
import os

from .ast_tree_profiler import AstTreeProfiler
from .util_static import modname_to_modpath, modpath_to_modname


def get_module_from_importfrom(node, module):
    r"""Resolve the full path of a relative import.

    Args:
        node (ast.ImportFrom)
            ImportFrom node
        module (str)
            Full dotted path relative to which the import is to occur

    Return:
        modname (str)
            Full path of the module from which the names are to be
            imported

    Example:
        >>> import ast
        >>> import functools
        >>> import textwrap
        >>>
        >>>
        >>> abs_import, *rel_imports = ast.parse(textwrap.dedent('''
        ... from a import b
        ... from . import b
        ... from .. import b
        ... from .baz import b
        ... from ..baz import b
        ... '''.strip('\n'))).body
        >>>
        >>>
        >>> get_module = functools.partial(
        ...     get_module_from_importfrom, module='foo.bar.foobar')
        >>> assert get_module(abs_import) == 'a'
        >>> assert get_module(rel_imports[0]) == 'foo.bar'
        >>> assert get_module(rel_imports[1]) == 'foo'
        >>> assert get_module(rel_imports[2]) == 'foo.bar.baz'
        >>> assert get_module(rel_imports[3]) == 'foo.baz'
    """
    level = node.level
    if not level:
        return node.module
    chunks = module.split('.')[:-level]
    if node.module:
        chunks.append(node.module)
    return '.'.join(chunks)


class ImportFromTransformer(ast.NodeTransformer):
    """Turn all the relative imports into absolute imports."""
    def __init__(self, module):
        self.module = module

    def visit_ImportFrom(self, node):
        level = node.level
        if not level:
            return self.generic_visit(node)
        module = get_module_from_importfrom(node, self.module)
        new_node = ast.ImportFrom(module=module, names=node.names, level=0)
        return self.generic_visit(ast.copy_location(new_node, node))


class AstTreeModuleProfiler(AstTreeProfiler):
    """Create an abstract syntax tree of an executable module and add
    profiling to it.

    Reads the module code and generates an abstract syntax tree, then adds nodes
    and/or decorators to the AST that adds the specified functions/methods,
    classes & modules in prof_mod to the profiler to be profiled.
    """
    @classmethod
    def _get_script_ast_tree(cls, script_file):
        tree = super()._get_script_ast_tree(script_file)
        # Note: don't drop the `.__init__` or `.__main__` suffix, lest
        # the relative imports fail
        module = modpath_to_modname(script_file,
                                    hide_main=False, hide_init=False)
        return ImportFromTransformer(module).visit(tree)

    @staticmethod
    def _is_main(fname):
        return os.path.basename(fname) == '__main__.py'

    @classmethod
    def _check_profile_full_script(cls, script_file, prof_mod):
        rp = os.path.realpath
        paths_to_check = {rp(script_file)}
        if cls._is_main(script_file):
            paths_to_check.add(rp(os.path.dirname(script_file)))
        paths_to_profile = {rp(mod) for mod in prof_mod}
        for mod in prof_mod:
            as_path = modname_to_modpath(mod)
            if as_path:
                paths_to_profile.add(rp(as_path))
        return bool(paths_to_check & paths_to_profile)
