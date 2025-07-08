from functools import cached_property, partial, partialmethod
from types import (CodeType, FunctionType, MethodType,
                   BuiltinFunctionType, BuiltinMethodType,
                   ClassMethodDescriptorType, MethodDescriptorType,
                   MethodWrapperType, WrapperDescriptorType)
from typing import (TYPE_CHECKING,
                    Any, Callable, Dict, List, Mapping, Protocol, TypeVar)
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
from ._line_profiler import label


T = TypeVar('T', bound=type)
T_co = TypeVar('T_co', covariant=True)
R = TypeVar('R')
PS = ParamSpec('PS')

if TYPE_CHECKING:
    class CythonCallable(Protocol[PS, T_co]):
        def __call__(self, *args: PS.args, **kwargs: PS.kwargs) -> T_co:
            ...

        @property
        def __code__(self) -> CodeType:
            ...

        @property
        def func_code(self) -> CodeType:
            ...

        @property
        def __name__(self) -> str:
            ...

        @property
        def func_name(self) -> str:
            ...

        @property
        def __qualname__(self) -> str:
            ...

        @property
        def __doc__(self) -> str | None:
            ...

        @__doc__.setter
        def __doc__(self, doc: str | None) -> None:
            ...

        @property
        def func_doc(self) -> str | None:
            ...

        @property
        def __globals__(self) -> Dict[str, Any]:
            ...

        @property
        def func_globals(self) -> Dict[str, Any]:
            ...

        @property
        def __dict__(self) -> Dict[str, Any]:
            ...

        @__dict__.setter
        def __dict__(self, dict: Dict[str, Any]) -> None:
            ...

        @property
        def func_dict(self) -> Dict[str, Any]:
            ...

        @property
        def __annotations__(self) -> Dict[str, Any]:
            ...

        @__annotations__.setter
        def __annotations__(self, annotations: Dict[str, Any]) -> None:
            ...

        @property
        def __defaults__(self):
            ...

        @property
        def func_defaults(self):
            ...

        @property
        def __kwdefaults__(self):
            ...

        @property
        def __closure__(self):
            ...

        @property
        def func_closure(self):
            ...


else:
    CythonCallable = type(label)

CLevelCallable = TypeVar('CLevelCallable',
                         BuiltinFunctionType, BuiltinMethodType,
                         ClassMethodDescriptorType, MethodDescriptorType,
                         MethodWrapperType, WrapperDescriptorType)


def is_c_level_callable(func: Any) -> TypeIs[CLevelCallable]:
    ...


def is_cython_callable(func: Any) -> TypeIs[CythonCallable]:
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
