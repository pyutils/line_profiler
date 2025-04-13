import ast

from .ast_tree_profiler import AstTreeProfiler


def get_module_from_importfrom(node: ast.ImportFrom, module: str) -> str:
    ...


class ImportFromTransformer(ast.NodeTransformer):
    def __init__(self, module: str) -> None:
        ...

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom:
        ...

    module: str


class AstTreeModuleProfiler(AstTreeProfiler):
    ...
