from IPython.core.magic import Magics
from . import LineProfiler


class LineProfilerMagics(Magics):
    def lprun(self, parameter_s: str = ...) -> LineProfiler | None:
        ...

    def lprun_all(self,
                  parameter_s: str = "",
                  cell: str = "") -> LineProfiler | None:
        ...
