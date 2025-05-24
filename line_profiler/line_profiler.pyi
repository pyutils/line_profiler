import io
from enum import auto
from functools import cached_property, partial, partialmethod
from inspect import isfunction as is_function
from types import (FunctionType, MethodType, ModuleType,
                   BuiltinFunctionType, BuiltinMethodType,
                   ClassMethodDescriptorType, MethodDescriptorType,
                   MethodWrapperType, WrapperDescriptorType)
from typing import (overload,
                    Any, Callable, List, Literal, Tuple, TypeVar, TypedDict)
try:
    from typing import (  # type: ignore[attr-defined]  # noqa: F401
        TypeIs)
except ImportError:  # Python < 3.13
    from typing_extensions import TypeIs  # noqa: F401
from _typeshed import Incomplete
from ._line_profiler import LineProfiler as CLineProfiler
from .line_profiler_utils import StringEnum
from .profiler_mixin import ByCountProfilerMixin


CLevelCallable = TypeVar('CLevelCallable',
                         BuiltinFunctionType, BuiltinMethodType,
                         ClassMethodDescriptorType, MethodDescriptorType,
                         MethodWrapperType, WrapperDescriptorType)
CallableLike = TypeVar('CallableLike',
                       FunctionType, partial, property, cached_property,
                       MethodType, staticmethod, classmethod, partialmethod)


def is_c_level_callable(func: Any) -> TypeIs[CLevelCallable]:
    ...


def load_ipython_extension(ip) -> None:
    ...


class ScopingPolicy(StringEnum):
    CHILDREN = auto()
    DESCENDANTS = auto()
    SIBLINGS = auto()
    NONE = auto()

    @overload
    def get_filter(
            self,
            namespace: type | ModuleType,
            obj_type: Literal['func']) -> Callable[[FunctionType], bool]:
        ...

    @overload
    def get_filter(
            self,
            namespace: type | ModuleType,
            obj_type: Literal['class']) -> Callable[[type], bool]:
        ...

    @overload
    def get_filter(
            self,
            namespace: type | ModuleType,
            obj_type: Literal['module']) -> Callable[[ModuleType], bool]:
        ...

    @classmethod
    def to_policies(
        cls,
        policies: (str | 'ScopingPolicy'
                   | ScopingPolicyDict | None) = None) -> _ScopingPolicyDict:
        ...


ScopingPolicyDict = TypedDict('ScopingPolicyDict',
                              {'func': str | ScopingPolicy,
                               'class': str | ScopingPolicy,
                               'module': str | ScopingPolicy})
_ScopingPolicyDict = TypedDict('_ScopingPolicyDict',
                               {'func': str | ScopingPolicy,
                                'class': str | ScopingPolicy,
                                'module': str | ScopingPolicy})


class LineProfiler(CLineProfiler, ByCountProfilerMixin):
    @overload
    def __call__(self,  # type: ignore[overload-overlap]
                 func: CLevelCallable) -> CLevelCallable:
        ...

    @overload
    def __call__(self,  # type: ignore[overload-overlap]
                 func: CallableLike) -> CallableLike:
        ...

    # Fallback: just wrap the `.__call__()` of a generic callable

    @overload
    def __call__(self, func: Callable) -> FunctionType:
        ...

    def add_callable(
            self, func, guard: (Callable[[FunctionType], bool]
                                | None) = None) -> Literal[0, 1]:
        ...

    def dump_stats(self, filename) -> None:
        ...

    def print_stats(self,
                    stream: Incomplete | None = ...,
                    output_unit: Incomplete | None = ...,
                    stripzeros: bool = ...,
                    details: bool = ...,
                    summarize: bool = ...,
                    sort: bool = ...,
                    rich: bool = ...) -> None:
        ...

    def add_module(
            self, mod: ModuleType, *,
            scoping_policy: (
                ScopingPolicy | str | ScopingPolicyDict | None) = None,
            wrap: bool = False) -> int:
        ...

    def add_class(
            self, cls: type, *,
            scoping_policy: (
                ScopingPolicy | str | ScopingPolicyDict | None) = None,
            wrap: bool = False) -> int:
        ...


def is_generated_code(filename):
    ...


def show_func(filename: str,
              start_lineno: int,
              func_name: str,
              timings: List[Tuple[int, int, float]],
              unit: float,
              output_unit: float | None = None,
              stream: io.TextIOBase | None = None,
              stripzeros: bool = False,
              rich: bool = False) -> None:
    ...


def show_text(stats,
              unit,
              output_unit: Incomplete | None = ...,
              stream: Incomplete | None = ...,
              stripzeros: bool = ...,
              details: bool = ...,
              summarize: bool = ...,
              sort: bool = ...,
              rich: bool = ...):
    ...


def load_stats(filename):
    ...


def main():
    ...
