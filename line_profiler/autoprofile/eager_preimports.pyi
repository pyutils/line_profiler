import ast
from typing import (
    Any, Union,
    Collection, Dict, Generator, List, NamedTuple, Set, Tuple,
    TextIO)


def is_dotted_path(obj: Any) -> bool:
    ...


def get_expression(obj: Any) -> Union[ast.Expression, None]:
    ...


def split_dotted_path(
        dotted_path: str, static: bool = True) -> Tuple[str, Union[str, None]]:
    ...


def strip(s: str) -> str:
    ...


class LoadedNameFinder(ast.NodeVisitor):
    names: Set[str]
    contexts: List[Set[str]]

    def visit_Name(self, node: ast.Name) -> None:
        ...

    def visit_FunctionDef(self,
                          node: Union[ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.Lambda]) -> None:
        ...

    visit_AsyncFunctionDef = visit_Lambda = visit_FunctionDef

    @classmethod
    def find(cls, node: ast.AST) -> Set[str]:
        ...


def propose_names(prefixes: Collection[str]) -> Generator[str, None, None]:
    ...


def resolve_profiling_targets(
        dotted_paths: Collection[str],
        static: bool = True,
        recurse: Union[Collection[str], bool] = False) -> 'ResolvedResult':
    ...


def write_eager_import_module(
        dotted_paths: Collection[str], stream: Union[TextIO, None] = None, *,
        static: bool = True,
        recurse: Union[Collection[str], bool] = False,
        adder: str = 'profile.add_imported_function_or_module',
        indent: str = '    ') -> None:
    ...


class ResolvedResult(NamedTuple):
    targets: Dict[str, Set[Union[str, None]]]
    indirect: Set[str]
    unresolved: List[str]
