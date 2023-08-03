"""
Line Profiler
=============

The line_profiler module for doing line-by-line profiling of functions

+---------------+-------------------------------------------+
| Github        | https://github.com/pyutils/line_profiler  |
+---------------+-------------------------------------------+
| Pypi          | https://pypi.org/project/line_profiler    |
+---------------+-------------------------------------------+


Installation
============

Releases of ``line_profiler`` can be installed using pip

.. code:: bash

    pip install line_profiler

"""
__submodules__ = [
    'line_profiler',
    'ipython_extension',
]

__autogen__ = """
mkinit ./line_profiler/__init__.py --relative
mkinit ./line_profiler/__init__.py --relative -w
"""


# from .line_profiler import __version__

# NOTE: This needs to be in sync with ../kernprof.py and line_profiler.py
__version__ = '4.1.0'

from .line_profiler import (LineProfiler,
                            load_ipython_extension, load_stats, main,
                            show_func, show_text,)


from .explicit_profiler import profile


__all__ = ['LineProfiler', 'line_profiler',
           'load_ipython_extension', 'load_stats', 'main', 'show_func',
           'show_text', '__version__', 'profile']
