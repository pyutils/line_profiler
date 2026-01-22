import io
from functools import cached_property, partial, partialmethod
from os import PathLike
from types import FunctionType, ModuleType
from typing import (TYPE_CHECKING,
                    overload,
                    Callable, Mapping, Sequence,
                    Literal, Self,
                    Protocol, TypeVar, ParamSpec)
from _typeshed import Incomplete
from ._line_profiler import (LineProfiler as CLineProfiler,
                             LineStats as CLineStats)
from .profiler_mixin import ByCountProfilerMixin, CLevelCallable
from .scoping_policy import ScopingPolicy, ScopingPolicyDict

if TYPE_CHECKING:
    from .profiler_mixin import UnparametrizedCallableLike


T = TypeVar('T')
T_co = TypeVar('T_co', covariant=True)
PS = ParamSpec('PS')
_TimingsMap = Mapping[tuple[str, int, str], list[tuple[int, int, int]]]


def get_column_widths(
    config: bool | str | PathLike[str] | None = False) -> Mapping[
        Literal['line', 'hits', 'time', 'perhit', 'percent'], int]:
    ...


def load_ipython_extension(ip) -> None:
    ...


class _StatsLike(Protocol):
    timings: _TimingsMap
    unit: float


class LineStats(CLineStats):
    def __init__(self, timings: _TimingsMap, unit: float) -> None:
        ...

    def to_file(self, filename: PathLike[str] | str) -> None:
        ...

    def print(
        self, stream: io.TextIOBase | None = None,
        output_unit: float | None = None,
        stripzeros: bool = False, details: bool = True,
        summarize: bool = False, sort: bool = False, rich: bool = False,
        *, config: str | PathLike[str] | bool | None = None) -> None:
        ...

    @classmethod
    def from_files(cls, file: PathLike[str] | str, /,
                   *files: PathLike[str] | str) -> Self:
        ...

    @classmethod
    def from_stats_objects(cls, stats: _StatsLike, /,
                           *more_stats: _StatsLike) -> Self:
        ...

    def __repr__(self) -> str:
        ...

    def __eq__(self, other) -> bool:
        ...

    def __add__(self, other: _StatsLike) -> Self:
        ...

    def __iadd__(self, other: _StatsLike) -> Self:
        ...


class LineProfiler(CLineProfiler, ByCountProfilerMixin):
    @overload
    def __call__(self,
                 func: CLevelCallable) -> CLevelCallable:
        ...

    @overload
    def __call__(
        self, func: UnparametrizedCallableLike,
    ) -> UnparametrizedCallableLike:
        ...

    @overload
    def __call__(self,
                 func: type[T]) -> type[T]:
        ...

    @overload
    def __call__(self,
                 func: partial[T]) -> partial[T]:
        ...

    @overload
    def __call__(self, func: partialmethod[T]) -> partialmethod[T]:
        ...

    @overload
    def __call__(self, func: cached_property[T_co]) -> cached_property[T_co]:
        ...

    @overload
    def __call__(self,
                 func: staticmethod[PS, T_co]) -> staticmethod[PS, T_co]:
        ...

    @overload
    def __call__(
        self, func: classmethod[type[T], PS, T_co],
    ) -> classmethod[type[T], PS, T_co]:
        ...

    # Fallback: just wrap the `.__call__()` of a generic callable

    @overload
    def __call__(self, func: Callable) -> Callable:
        ...

    def add_callable(
            self, func,
            guard: Callable[[FunctionType], bool] | None = None,
            name: str | None = None) -> Literal[0, 1]:
        ...

    def get_stats(self) -> LineStats:
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
              timings: Sequence[tuple[int, int, int | float]],
              unit: float,
              output_unit: float | None = None,
              stream: io.TextIOBase | None = None,
              stripzeros: bool = False,
              rich: bool = False,
              *,
              config: str | PathLike[str] | bool | None = None) -> None:
    ...


def show_text(stats: _TimingsMap,
              unit: float,
              output_unit: float | None = ...,
              stream: io.TextIOBase | None = ...,
              stripzeros: bool = ...,
              details: bool = ...,
              summarize: bool = ...,
              sort: bool = ...,
              rich: bool = ...,
              *,
              config: str | PathLike[str] | bool | None = None) -> None:
    ...


load_stats = LineStats.from_files


def main():
    ...
