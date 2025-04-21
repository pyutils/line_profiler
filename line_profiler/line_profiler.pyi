from types import ModuleType
from typing import Literal, List, Tuple
import io
from ._line_profiler import LineProfiler as CLineProfiler
from .profiler_mixin import ByCountProfilerMixin
from _typeshed import Incomplete


def load_ipython_extension(ip) -> None:
    ...


class LineProfiler(CLineProfiler, ByCountProfilerMixin):
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

    def add_module(self, mod: ModuleType, *, wrap: bool = False) -> int:
        ...

    def add_class(self, cls: type, *, wrap: bool = False) -> int:
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
