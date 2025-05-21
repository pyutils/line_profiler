from types import CodeType, ModuleType, FunctionType
from typing import Literal, Dict, List, Tuple
import io
from ._line_profiler import LineStats
from .profiler_mixin import ByCountProfilerMixin
from _typeshed import Incomplete


def load_ipython_extension(ip) -> None:
    ...


class LineProfiler(ByCountProfilerMixin):
    def __init__(self, *functions: FunctionType):
        ...

    def __call__(self, func):
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

    def add_module(self, mod: ModuleType) -> int:
        ...

    # `line_profiler._line_profiler.LineProfiler` methods and attributes
    # (note: some of them are properties because they wrap around the
    # corresponding C-level profiler attribute, but are bare attributes
    # on the C-level profiler)

    functions: List[FunctionType]

    def add_function(self, func: FunctionType) -> None:
        ...

    def enable_by_count(self) -> None:
        ...

    def disable_by_count(self) -> None:
        ...

    def enable(self) -> None:
        ...

    def disable(self) -> None:
        ...

    def get_stats(self) -> LineStats:
        ...

    @property
    def code_hash_map(self) -> Dict[CodeType, List[int]]:
        ...

    @property
    def dupes_map(self) -> Dict[bytes, List[CodeType]]:
        ...

    @property
    def c_code_map(self) -> Dict[int, dict]:
        ...

    @property
    def c_last_time(self) -> Dict[int, dict]:
        ...

    @property
    def code_map(self) -> Dict[CodeType, Dict[int, dict]]:
        ...

    @property
    def last_time(self) -> Dict[CodeType, dict]:
        ...

    @property
    def enable_count(self) -> int:
        ...

    @enable_count.setter
    def enable_count(self, value: int) -> None:
        ...

    @property
    def timer_unit(self) -> float:
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
