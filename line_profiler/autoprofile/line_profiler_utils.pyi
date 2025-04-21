from typing import Callable, Literal
from types import ModuleType


def add_imported_function_or_module(
        self, item: Callable | type | ModuleType, *,
        wrap: bool = False) -> Literal[0, 1]:
    ...
