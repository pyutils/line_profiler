from functools import cached_property, partial, partialmethod
from inspect import isfunction as is_function
from types import (FunctionType, MethodType, ModuleType,
                   BuiltinFunctionType, BuiltinMethodType,
                   ClassMethodDescriptorType, MethodDescriptorType,
                   MethodWrapperType, WrapperDescriptorType)
from typing import (overload,
                    Any, Literal, Callable, List, Tuple, TypeGuard, TypeVar)
import io
from ._line_profiler import LineProfiler as CLineProfiler
from .profiler_mixin import ByCountProfilerMixin
from _typeshed import Incomplete


CLevelCallable = TypeVar('CLevelCallable',
                         BuiltinFunctionType, BuiltinMethodType,
                         ClassMethodDescriptorType, MethodDescriptorType,
                         MethodWrapperType, WrapperDescriptorType)
CallableLike = TypeVar('CallableLike',
                       FunctionType, partial, property, cached_property,
                       MethodType, staticmethod, classmethod, partialmethod)
MatchScopeOption = Literal['exact', 'descendants', 'siblings', 'none']


def is_c_level_callable(func: Any) -> TypeGuard[CLevelCallable]:
    ...


def load_ipython_extension(ip) -> None:
    ...


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

    def add_callable(self, func) -> Literal[0, 1]:
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

    def add_module(self, mod: ModuleType, *,
                   match_scope: MatchScopeOption = 'siblings',
                   wrap: bool = False) -> int:
        ...

    def add_class(self, cls: type, *,
                  match_scope: MatchScopeOption = 'siblings',
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
