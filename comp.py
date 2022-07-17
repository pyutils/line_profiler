import multiprocessing
import os
import sys
from setuptools import Extension, setup

from Cython.Build import cythonize

def run_cythonize(force=False):
    return cythonize(
            Extension(
                name="_line_profiler",
                sources=[f"line_profiler/_line_profiler.pyx", "line_profiler/timers.c", "line_profiler/unset_trace.c"],
                language="c++",
                define_macros=[("CYTHON_TRACE", (1 if os.getenv("DEV") == "true" else 0))],
            ),
            compiler_directives={"language_level": 3, "infer_types": True, "linetrace": (True if os.getenv("DEV") == "true" else False)},
            force=force,
            nthreads=multiprocessing.cpu_count(),
        )

def run_setup(force=False):
    setup(
        ext_modules=run_cythonize()
    )


if __name__ == "__main__":
    run_setup(force=True)
