from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

class LineStats:
    timings: Mapping[tuple[str, int, str], list[tuple[int, int, int]]]
    unit: float

    def __init__(
        self,
        timings: Mapping[tuple[str, int, str], list[tuple[int, int, int]]],
        unit: float,
    ) -> None: ...

class LineProfiler:
    def __init__(
        self,
        # Note: realistically these should be `types.FunctionType` or
        # `MethodType`, but type-annotating this as those results in
        # type checkers balking against even the most simple of usecases
        # because bare functions are resolved to `Callable`s with the
        # appropriate signatures, and are no longer recognized as
        # `FunctionType`s.
        *functions: Callable[..., Any],
        wrap_trace: bool | None = None,
        set_frame_local_trace: bool | None = None,
    ) -> None:
        ...

    def enable_by_count(self) -> None: ...
    def disable_by_count(self) -> None: ...
    def add_function(self, func: Any) -> None: ...
    def get_stats(self) -> LineStats: ...
    def dump_stats(self, filename: str) -> None: ...

def label(code: Any) -> Any: ...
