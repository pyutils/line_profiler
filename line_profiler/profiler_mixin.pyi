from functools import cached_property, partial, partialmethod
from types import (CodeType, FunctionType, MethodType,
                   BuiltinFunctionType, BuiltinMethodType,
                   ClassMethodDescriptorType, MethodDescriptorType,
                   MethodWrapperType, WrapperDescriptorType)
from typing import (TYPE_CHECKING, overload,
                    Any, Callable, Mapping, Protocol, TypeVar)
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


UnparametrizedCallableLike = TypeVar('UnparametrizedCallableLike',
                                     FunctionType, property, MethodType)
T = TypeVar('T')
T_co = TypeVar('T_co', covariant=True)
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
        def __globals__(self) -> dict[str, Any]:
            ...

        @property
        def func_globals(self) -> dict[str, Any]:
            ...

        @property
        def __dict__(self) -> dict[str, Any]:
            ...

        @__dict__.setter
        def __dict__(self, dict: dict[str, Any]) -> None:
            ...

        @property
        def func_dict(self) -> dict[str, Any]:
            ...

        @property
        def __annotations__(self) -> dict[str, Any]:
            ...

        @__annotations__.setter
        def __annotations__(self, annotations: dict[str, Any]) -> None:
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
    def get_underlying_functions(self, func) -> list[FunctionType]:
        ...

    @overload
    def wrap_callable(self,  # type: ignore[overload-overlap]
                      func: CLevelCallable) -> CLevelCallable:
        ...

    @overload
    def wrap_callable(  # type: ignore[overload-overlap]
        self, func: UnparametrizedCallableLike,
    ) -> UnparametrizedCallableLike:
        ...

    @overload
    def wrap_callable(self,  # type: ignore[overload-overlap]
                      func: type[T]) -> type[T]:
        ...

    @overload
    def wrap_callable(self,  # type: ignore[overload-overlap]
                      func: partial[T]) -> partial[T]:
        ...

    @overload
    def wrap_callable(self, func: partialmethod[T]) -> partialmethod[T]:
        ...

    @overload
    def wrap_callable(self,
                      func: cached_property[T_co]) -> cached_property[T_co]:
        ...

    @overload
    def wrap_callable(self,  # type: ignore[overload-overlap]
                      func: staticmethod[PS, T_co]) -> staticmethod[PS, T_co]:
        ...

    @overload
    def wrap_callable(
        self, func: classmethod[type[T], PS, T_co],
    ) -> classmethod[type[T], PS, T_co]:
        ...

    # Fallback: just return a wrapper function around a generic callable

    @overload
    def wrap_callable(self, func: Callable) -> FunctionType:
        ...

    def wrap_classmethod(
        self, func: classmethod[type[T], PS, T_co],
    ) -> classmethod[type[T], PS, T_co]:
        ...

    def wrap_staticmethod(
            self, func: staticmethod[PS, T_co]) -> staticmethod[PS, T_co]:
        ...

    def wrap_boundmethod(self, func: MethodType) -> MethodType:
        ...

    def wrap_partialmethod(self, func: partialmethod[T]) -> partialmethod[T]:
        ...

    def wrap_partial(self, func: partial[T]) -> partial[T]:
        ...

    def wrap_property(self, func: property) -> property:
        ...

    def wrap_cached_property(
            self, func: cached_property[T_co]) -> cached_property[T_co]:
        ...

    def wrap_async_generator(self, func: FunctionType) -> FunctionType:
        ...

    def wrap_coroutine(self, func: FunctionType) -> FunctionType:
        ...

    def wrap_generator(self, func: FunctionType) -> FunctionType:
        ...

    def wrap_function(self, func: Callable) -> FunctionType:
        ...

    def wrap_class(self, func: type[T]) -> type[T]:
        ...

    def run(self, cmd: str) -> Self:
        ...

    def runctx(self,
               cmd: str,
               globals: dict[str, Any] | None,
               locals: Mapping[str, Any] | None) -> Self:
        ...

    def runcall(self, func: Callable[PS, T], /,
                *args: PS.args, **kw: PS.kwargs) -> T:
        ...

    def __enter__(self) -> Self:
        ...

    def __exit__(self, *_, **__) -> None:
        ...
