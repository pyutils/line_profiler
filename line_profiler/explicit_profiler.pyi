from typing import Callable
from _typeshed import Incomplete


class GlobalProfiler:
    output_prefix: str
    environ_flag: str
    cli_flags: Incomplete
    enabled: Incomplete

    def __init__(self) -> None:
        ...

    def implicit_setup(self) -> None:
        ...

    def enable(self, output_prefix: Incomplete | None = ...) -> None:
        ...

    def disable(self) -> None:
        ...

    def __call__(self, func: Callable) -> Callable:
        ...

    def show(self) -> None:
        ...


profile: Incomplete
