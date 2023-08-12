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




Demo
----

The following code gives a demonstration:

First we generate a demo python script:

.. code:: bash

    python -c "if 1:
        import textwrap

        # Define the script
        text = textwrap.dedent(
            '''
            from line_profiler import profile

            @profile
            def plus(a, b):
                return a + b

            @profile
            def fib(n):
                a, b = 0, 1
                while a < n:
                    a, b = b, plus(a, b)

            @profile
            def main():
                import math
                import time
                start = time.time()

                print('start calculating')
                while time.time() - start < 1:
                    fib(10)
                    math.factorial(1000)
                print('done calculating')

            main()
            '''
        ).strip()

        # Write the script to disk
        with open('demo_script.py', 'w') as file:
            file.write(text)
    "


Run the script with kernprof

.. code:: bash

    python -m kernprof demo_script.py

.. code:: bash

    python -m pstats demo_script.py.prof



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
