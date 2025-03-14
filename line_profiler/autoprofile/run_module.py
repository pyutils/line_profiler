import ast
import os

from .ast_tree_profiler import AstTreeProfiler
from .util_static import modname_to_modpath


def get_module_from_importfrom(node, module, main=False):
    r"""Resolve the full path of a relative import.

    Args:
        node (ast.ImportFrom)
            ImportFrom node
        module (str)
            Full path relative to which the import is to occur
        main (bool)
            Whether the node originated from a '__main__.py' file

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
        >>> get_submodule = functools.partial(
        ...     get_module_from_importfrom, module='foo.bar.foobar')
        >>> get_module_main = functools.partial(
        ...     get_module_from_importfrom,
        ...     module='foo.bar',
        ...     main=True)
        >>> for get_module in get_submodule, get_module_main:
        ...     assert get_module(abs_import) == 'a'
        ...     assert get_module(rel_imports[0]) == 'foo.bar'
        ...     assert get_module(rel_imports[1]) == 'foo'
        ...     assert get_module(rel_imports[2]) == 'foo.bar.baz'
        ...     assert get_module(rel_imports[3]) == 'foo.baz'
    """
    level = node.level
    if not level:
        return node.module
    if main:
        level -= 1
    if level:
        module = '.'.join(module.split('.')[:-level])
    if node.module:
        return module + '.' + node.module
    return module


class ImportFromTransformer(ast.NodeTransformer):
    """Turn all the relative imports into absolute imports."""
    def __init__(self, module, main=False):
        self.module = module
        self.main = main

    def visit_ImportFrom(self, node):
        level = node.level
        if not level:
            return self.generic_visit(node)
        module = get_module_from_importfrom(node, self.module, self.main)
        new_node = ast.ImportFrom(module=module,
                                  names=node.names,
                                  level=0)
        return self.generic_visit(ast.copy_location(new_node, node))


class AstTreeModuleProfiler(AstTreeProfiler):
    """Create an abstract syntax tree of an executable module and add
    profiling to it.

    Reads the module code and generates an abstract syntax tree, then adds nodes
    and/or decorators to the AST that adds the specified functions/methods,
    classes & modules in prof_mod to the profiler to be profiled.
    """
    def __init__(self, module_file, module_name, *args, **kwargs):
        """Initializes the AST tree profiler instance with the executable module
        file and module name.

        Args:
            module_file (str):
                the module file or its `__main__.py` if a package.

            module_name (str):
                name of the module being profiled.

            *args, **kwargs:
                passed to `.ast_tree_profiler.AstTreeProfiler`.
        """
        self._module = module_name
        self._main = self._is_main(module_file)
        super().__init__(module_file, *args, **kwargs)

    def _get_script_ast_tree(self, script_file):
        tree = super()._get_script_ast_tree(script_file)
        return ImportFromTransformer(self._module, self._main).visit(tree)

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
