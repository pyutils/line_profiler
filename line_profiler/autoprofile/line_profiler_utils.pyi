from types import ModuleType
from typing import overload, Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:  # Stub-only annotations
    from ..line_profiler import CLevelCallable, CallableLike, MatchScopeOption


@overload
def add_imported_function_or_module(
        self, item: CLevelCallable | Any,
        match_scope: MatchScopeOption = 'siblings',
        wrap: bool = False) -> Literal[0]:
    ...


@overload
def add_imported_function_or_module(
        self, item: CallableLike | type | ModuleType,
        match_scope: MatchScopeOption = 'siblings',
        wrap: bool = False) -> Literal[0, 1]:
    ...
