from types import ModuleType
from typing import overload, Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:  # Stub-only annotations
    from ..line_profiler import CallableLike
    from ..profiler_mixin import CLevelCallable
    from ..scoping_policy import ScopingPolicy, ScopingPolicyDict


@overload
def add_imported_function_or_module(
        self, item: CLevelCallable | Any,
        scoping_policy: (
            ScopingPolicy | str | ScopingPolicyDict | None) = None,
        wrap: bool = False) -> Literal[0]:
    ...


@overload
def add_imported_function_or_module(
        self, item: CallableLike | ModuleType,
        scoping_policy: (
            ScopingPolicy | str | ScopingPolicyDict | None) = None,
        wrap: bool = False) -> Literal[0, 1]:
    ...
