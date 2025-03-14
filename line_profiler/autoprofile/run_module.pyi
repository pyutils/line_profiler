import _ast

from .ast_tree_profiler import AstTreeProfiler


def get_module_from_importfrom(node: _ast.ImportFrom, module: str) -> str:
    ...


class ImportFromTransformer(_ast.NodeTransformer):
    def __init__(self, module: str, main: bool = False) -> None:
        ...

    def visit_ImportFrom(self, _ast.ImportFrom) -> _ast.ImportFrom:
        ...

    module: str
    main: bool


class AstTreeModuleProfiler(AstTreeProfiler):
    def __init__(self, module_file: str, module_name: str, *args, **kwargs):
        ...

    _module: str
    _main: bool
