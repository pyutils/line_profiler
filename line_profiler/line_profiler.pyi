import io
import pathlib
from functools import cached_property, partial, partialmethod
from os import PathLike
from types import FunctionType, ModuleType
from typing import TYPE_CHECKING, overload, Callable, Literal, Mapping, TypeVar
try:
    from typing import (  # type: ignore[attr-defined]  # noqa: F401
        ParamSpec)
except ImportError:
    from typing_extensions import ParamSpec  # noqa: F401
from _typeshed import Incomplete
from ._line_profiler import LineProfiler as CLineProfiler
from .profiler_mixin import ByCountProfilerMixin, CLevelCallable
from .scoping_policy import ScopingPolicy, ScopingPolicyDict

if TYPE_CHECKING:
    from .profiler_mixin import UnparametrizedCallableLike


T = TypeVar('T')
T_co = TypeVar('T_co', covariant=True)
PS = ParamSpec('PS')


def get_column_widths(
    config: bool | str | PathLike[str] | None = False) -> Mapping[
        Literal['line', 'hits', 'time', 'perhit', 'percent'], int]:
    ...


def load_ipython_extension(ip) -> None:
    ...


class LineProfiler(CLineProfiler, ByCountProfilerMixin):
    @overload
    def __call__(self,  # type: ignore[overload-overlap]
                 func: CLevelCallable) -> CLevelCallable:
        ...

    @overload
    def __call__(  # type: ignore[overload-overlap]
        self, func: UnparametrizedCallableLike,
    ) -> UnparametrizedCallableLike:
        ...

    @overload
    def __call__(self,  # type: ignore[overload-overlap]
                 func: type[T]) -> type[T]:
        ...

    @overload
    def __call__(self,  # type: ignore[overload-overlap]
                 func: partial[T]) -> partial[T]:
        ...

    @overload
    def __call__(self, func: partialmethod[T]) -> partialmethod[T]:
        ...

    @overload
    def __call__(self, func: cached_property[T_co]) -> cached_property[T_co]:
        ...

    @overload
    def __call__(self,  # type: ignore[overload-overlap]
                 func: staticmethod[PS, T_co]) -> staticmethod[PS, T_co]:
        ...

    @overload
    def __call__(
        self, func: classmethod[type[T], PS, T_co],
    ) -> classmethod[type[T], PS, T_co]:
        ...

    # Fallback: just wrap the `.__call__()` of a generic callable

    @overload
    def __call__(self, func: Callable) -> FunctionType:
        ...

    def add_callable(
            self, func,
            guard: Callable[[FunctionType], bool] | None = None,
            name: str | None = None) -> Literal[0, 1]:
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
                    rich: bool = ...,
                    *,
                    config: str | PathLike[str] | bool | None = None) -> None:
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
              timings: list[tuple[int, int, float]],
              unit: float,
              output_unit: float | None = None,
              stream: io.TextIOBase | None = None,
              stripzeros: bool = False,
              rich: bool = False,
              *,
              config: str | PathLike[str] | bool | None = None) -> None:
    ...


def show_text(stats,
              unit,
              output_unit: Incomplete | None = ...,
              stream: Incomplete | None = ...,
              stripzeros: bool = ...,
              details: bool = ...,
              summarize: bool = ...,
              sort: bool = ...,
              rich: bool = ...,
              *,
              config: str | PathLike[str] | bool | None = None) -> None:
    ...


def load_stats(filename):
    ...


def main():
    ...
