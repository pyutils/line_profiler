from functools import partial, partialmethod, cached_property
from types import FunctionType, MethodType, ModuleType
from typing import overload, Any, Literal, TypeVar, TYPE_CHECKING

if TYPE_CHECKING:  # Stub-only annotations
    from ..profiler_mixin import CLevelCallable, CythonCallable
    from ..scoping_policy import ScopingPolicy, ScopingPolicyDict




@overload
def add_imported_function_or_module(
        self, item: CLevelCallable | Any,
        scoping_policy: ScopingPolicy | str | ScopingPolicyDict | None = None,
        wrap: bool = False) -> Literal[0]:
    ...


@overload
def add_imported_function_or_module(
        self,
        item: (FunctionType | CythonCallable
               | type | partial | property | cached_property
               | MethodType | staticmethod | classmethod | partialmethod
               | ModuleType),
        scoping_policy: ScopingPolicy | str | ScopingPolicyDict | None = None,
        wrap: bool = False) -> Literal[0, 1]:
    ...
