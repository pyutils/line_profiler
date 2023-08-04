"""
The idea is that we are going to expose a top-level ``profile`` decorator which
will be disabled by default **unless** you are running with with line profiler
itself OR if the LINE_PROFILE environment variable is True.

This uses the atexit module to perform a profile dump at the end.

This also exposes a ``profile_now`` function, which displays stats every time a
function is run.

This work is ported from :mod:`xdev`.


Basic usage is to import line_profiler and decorate your function with
line_profiler.profile.  By default this does nothing, it's a no-op decorator.
However, if you run with ``LINE_PROFILER=1`` or ``'--profile' in sys.argv'``,
then it enables profiling and at the end of your script it will output the
profile text.

Here is a minimal example:

.. code:: bash

    # Write demo python script to disk
    python -c "if 1:
        import textwrap
        text = textwrap.dedent(
            '''
            from line_profiler import profile

            @profile
            def fib(n):
                a, b = 0, 1
                while a < n:
                    a, b = b, a + b

            fib(100)
            '''
        ).strip()
        with open('demo.py', 'w') as file:
            file.write(text)
    "

    echo "---"
    echo "## Base Case: Run without any profiling"
    python demo.py

    echo "---"
    echo "## Option 0: Original Usage"
    python -m kernprof -lv demo.py
    python -m line_profiler demo.py.lprof

    echo "---"
    echo "## Option 1: Enable profiler with the command line"
    python demo.py --line-profile

    echo "---"
    echo "## Option 1: Enable profiler with an environment variable"
    LINE_PROFILE=1 python demo.py

"""
from .line_profiler import LineProfiler
import sys
import os
import atexit


# For pyi autogeneration
__docstubs__ = """
# Note: xdev docstubs outputs incorrect code.
# For now just manually fix the resulting pyi file to shift these lines down
# and remove the extra incomplete profile type declaration

from .line_profiler import LineProfiler
from typing import Union
profile: Union[NoOpProfiler, LineProfiler]
"""


_FALSY_STRINGS = {'', '0', 'off', 'false', 'no'}

IS_PROFILING: bool
IS_PROFILING = os.environ.get('LINE_PROFILE', '').lower() not in _FALSY_STRINGS
IS_PROFILING = IS_PROFILING or ('--line-profile' in sys.argv) or ('--line_profile' in sys.argv)


OUTPUT_PREFIX = 'profile_output'


class NoOpProfiler:
    """
    A LineProfiler-like API that does nothing.
    """

    def __call__(self, func):
        """
        Args:
            func (Callable): function to decorate

        Returns:
            Callable: returns the input
        """
        return func

    def print_stats(self):
        print('Profiling was not enabled')


# Construct the global profiler. This is usually a NoOpProfiler unless the user
# requested the real one.
# NOTE: kernprof may overwrite this global
if IS_PROFILING:
    profile = LineProfiler()  # type: ignore
else:
    profile = NoOpProfiler()  # type: ignore


@atexit.register
def _show_profile_on_end():
    # if we are profiling, then dump out info at the end of the program
    if IS_PROFILING:
        import io
        from datetime import datetime as datetime_cls
        import pathlib
        stream = io.StringIO()
        profile.print_stats(stream=stream, summarize=1, sort=1, stripzeros=1)
        text = stream.getvalue()

        # TODO: highlight the code separately from the rest of the text
        try:
            from rich import print as rich_print
        except ImportError:
            rich_print = print
        rich_print(text)

        now = datetime_cls.now()
        timestamp = now.strftime('%Y-%m-%dT%H%M%S')

        lprof_output_fpath = pathlib.Path(f'{OUTPUT_PREFIX}.lprof')
        txt_output_fpath1 = pathlib.Path(f'{OUTPUT_PREFIX}.txt')
        txt_output_fpath2 = pathlib.Path(f'{OUTPUT_PREFIX}_{timestamp}.txt')

        txt_output_fpath1.write_text(text)
        txt_output_fpath2.write_text(text)
        profile.dump_stats(lprof_output_fpath)
        print('Wrote profile results to %s' % lprof_output_fpath)
