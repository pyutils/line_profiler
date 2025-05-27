import io
from functools import cached_property, partial, partialmethod
from types import FunctionType, MethodType, ModuleType
from typing import overload, Callable, List, Literal, Tuple, TypeVar
from _typeshed import Incomplete
from ._line_profiler import LineProfiler as CLineProfiler
from .profiler_mixin import ByCountProfilerMixin, CLevelCallable
from .scoping_policy import ScopingPolicy, ScopingPolicyDict


CallableLike = TypeVar('CallableLike',
                       FunctionType, partial, property, cached_property,
                       MethodType, staticmethod, classmethod, partialmethod,
                       type)


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
