from typing import Callable

# Note: xdev docstubs outputs incorrect code.
# For now just manually fix the resulting pyi file to shift these lines down
# and remove the extra incomplete profile type declaration

from .line_profiler import LineProfiler
from typing import Union

__docstubs__: str
IS_PROFILING: bool


class NoOpProfiler:

    def __call__(self, func: Callable) -> Callable:
        ...

    def print_stats(self) -> None:
        ...


profile: Union[NoOpProfiler, LineProfiler]
