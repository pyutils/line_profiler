from _typeshed import Incomplete

IS_PROFILING: Incomplete


class NoOpProfiler:

    def __call__(self, func):
        ...

    def print_stats(self) -> None:
        ...


profile: Incomplete
