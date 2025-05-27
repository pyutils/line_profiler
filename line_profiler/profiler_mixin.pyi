from functools import cached_property, partial, partialmethod
from types import (FunctionType, MethodType,
                   BuiltinFunctionType, BuiltinMethodType,
                   ClassMethodDescriptorType, MethodDescriptorType,
                   MethodWrapperType, WrapperDescriptorType)
from typing import Any, Callable, Dict, List, Mapping, TypeVar
try:
    from typing import (  # type: ignore[attr-defined]  # noqa: F401
        ParamSpec)
except ImportError:  # Python < 3.10
    from typing_extensions import ParamSpec  # noqa: F401
try:
    from typing import (  # type: ignore[attr-defined]  # noqa: F401
        Self)
except ImportError:  # Python < 3.11
    from typing_extensions import Self  # noqa: F401
try:
    from typing import (  # type: ignore[attr-defined]  # noqa: F401
        TypeIs)
except ImportError:  # Python < 3.13
    from typing_extensions import TypeIs  # noqa: F401


CLevelCallable = TypeVar('CLevelCallable',
                         BuiltinFunctionType, BuiltinMethodType,
                         ClassMethodDescriptorType, MethodDescriptorType,
                         MethodWrapperType, WrapperDescriptorType)
T = TypeVar('T', bound=type)
R = TypeVar('R')
PS = ParamSpec('PS')


def is_c_level_callable(func: Any) -> TypeIs[CLevelCallable]:
    ...


def is_classmethod(f: Any) -> TypeIs[classmethod]:
    ...


def is_staticmethod(f: Any) -> TypeIs[staticmethod]:
    ...


def is_boundmethod(f: Any) -> TypeIs[MethodType]:
    ...


def is_partialmethod(f: Any) -> TypeIs[partialmethod]:
    ...


def is_partial(f: Any) -> TypeIs[partial]:
    ...


def is_property(f: Any) -> TypeIs[property]:
    ...


def is_cached_property(f: Any) -> TypeIs[cached_property]:
    ...


class ByCountProfilerMixin:
    def get_underlying_functions(self, func) -> List[FunctionType]:
        ...

    def wrap_callable(self, func):
        ...

    def wrap_classmethod(self, func: classmethod) -> classmethod:
        ...

    def wrap_staticmethod(self, func: staticmethod) -> staticmethod:
        ...

    def wrap_boundmethod(self, func: MethodType) -> MethodType:
        ...

    def wrap_partialmethod(self, func: partialmethod) -> partialmethod:
        ...

    def wrap_partial(self, func: partial) -> partial:
        ...

    def wrap_property(self, func: property) -> property:
        ...

    def wrap_cached_property(self, func: cached_property) -> cached_property:
        ...

    def wrap_async_generator(self, func: FunctionType) -> FunctionType:
        ...

    def wrap_coroutine(self, func: FunctionType) -> FunctionType:
        ...

    def wrap_generator(self, func: FunctionType) -> FunctionType:
        ...

    def wrap_function(self, func: Callable) -> FunctionType:
        ...

    def wrap_class(self, func: T) -> T:
        ...

    def run(self, cmd: str) -> Self:
        ...

    def runctx(self,
               cmd: str,
               globals: Dict[str, Any] | None,
               locals: Mapping[str, Any] | None) -> Self:
        ...

    def runcall(self, func: Callable[PS, R], /,
                *args: PS.args, **kw: PS.kwargs) -> R:
        ...

    def __enter__(self) -> Self:
        ...

    def __exit__(self, *_, **__) -> None:
        ...
