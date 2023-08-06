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
    python -m kernprof -l demo.py
    python -m line_profiler demo.py.lprof

    echo "---"
    echo "## Option 1: Enable profiler with the command line"
    python demo.py --line-profile

    echo "---"
    echo "## Option 1: Enable profiler with an environment variable"
    LINE_PROFILE=1 python demo.py


.. code:: bash

    # In-code enabling
    python -c "if 1:
        import textwrap
        text = textwrap.dedent(
            '''
            from line_profiler import profile
            profile.enable(output_prefix='customized')

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
    echo "## Configuration handled inside the script"
    python demo.py

    # In-code enabling / disable
    python -c "if 1:
        import textwrap
        text = textwrap.dedent(
            '''
            from line_profiler import profile

            @profile
            def func1():
                return list(range(100))

            profile.enable(output_prefix='custom')

            @profile
            def func2():
                return tuple(range(100))

            profile.disable()

            @profile
            def func3():
                return set(range(100))

            profile.enable()

            @profile
            def func4():
                return dict(zip(range(100), range(100)))

            print(type(func1()))
            print(type(func2()))
            print(type(func3()))
            print(type(func4()))
            '''
        ).strip()
        with open('demo.py', 'w') as file:
            file.write(text)
    "

    echo "---"
    echo "## Configuration handled inside the script"
    python demo.py
    python demo.py --line-profile
"""
from .line_profiler import LineProfiler
import sys
import os
import atexit


# For pyi autogeneration
# xdev docstubs line_profiler
# __docstubs__ = """
# # Note: xdev docstubs outputs incorrect code.
# # For now just manually fix the resulting pyi file to shift these lines down
# # and remove the extra incomplete profile type declaration

# from .line_profiler import LineProfiler
# from typing import Union
# profile: Union[NoOpProfiler, LineProfiler]
# """


_FALSY_STRINGS = {'', '0', 'off', 'false', 'no'}


class GlobalProfiler:
    """
    Manages a profiler that will output on interpreter exit.
    """

    def __init__(self):
        self.output_prefix = 'profile_output'
        self.environ_flag = 'LINE_PROFILE'
        self.cli_flags = ['--line-profile', '--line_profile']
        self._profile = None
        self.enabled = None

    def _kernprof_overwrite(self, profile):
        """
        Kernprof will call this when it runs, so we can use its profile object
        instead of our own. Note: when kernprof overwrites us we wont ever
        register an atexit hook. This is what we want because kernprof wants us
        to use another program to read its output file.
        """
        self._profile = profile

    def _implicit_setup(self):
        """
        Called once the first time the user decorates a function with
        ``line_profiler.profile`` and they have not explicitly setup the global
        profiling options.
        """
        is_profiling = os.environ.get(self.environ_flag, '').lower() not in _FALSY_STRINGS
        is_profiling = is_profiling or any(f in sys.argv for f in self.cli_flags)
        if is_profiling:
            self.enable()
        else:
            self.disable()

    def enable(self, output_prefix=None):
        """
        Explicitly enables global profiler and controls its settings.
        """
        if self._profile is None:
            # Try to only ever create one real LineProfiler object
            atexit.register(self.show)
            self._profile = LineProfiler()  # type: ignore

        # The user can call this function more than once to update the final
        # reporting or to re-enable the profiler after it a disable.
        self.enabled = True

        if output_prefix is not None:
            self.output_prefix = output_prefix

    def disable(self):
        """
        Explicitly initialize a disabled global profiler.
        """
        self.enabled = False

    def __call__(self, func):
        """
        If the global profiler is enabled, decorate a function to start the
        profiler on function entry and stop it on function exit. Otherwise
        return the input.

        Args:
            func (Callable): the function to profile

        Returns:
            Callable: a potentially wrapped function
        """
        if self.enabled is None:
            # Force a setup if we haven't done it before.
            self._implicit_setup()
        if not self.enabled:
            return func
        return self._profile(func)

    def show(self):
        """
        Should only be called by atexit, and only if the implicit setup was
        used.
        """
        import io
        from datetime import datetime as datetime_cls
        import pathlib

        stream = io.StringIO()
        self._profile.print_stats(stream=stream, summarize=1, sort=1, stripzeros=1, rich=1)
        rich_text = stream.getvalue()

        stream = io.StringIO()
        self._profile.print_stats(stream=stream, summarize=1, sort=1, stripzeros=1, rich=0)
        raw_text = stream.getvalue()

        # TODO: highlight the code separately from the rest of the text
        # try:
        #     from rich import print as rich_print
        # except ImportError:
        #     rich_print = print
        # rich_print(text)
        print(rich_text)

        now = datetime_cls.now()
        timestamp = now.strftime('%Y-%m-%dT%H%M%S')

        lprof_output_fpath = pathlib.Path(f'{self.output_prefix}.lprof')
        txt_output_fpath1 = pathlib.Path(f'{self.output_prefix}.txt')
        txt_output_fpath2 = pathlib.Path(f'{self.output_prefix}_{timestamp}.txt')

        txt_output_fpath1.write_text(raw_text)
        txt_output_fpath2.write_text(raw_text)
        self._profile.dump_stats(lprof_output_fpath)
        print('Wrote profile results to %s' % lprof_output_fpath)
        print('Wrote profile results to %s' % txt_output_fpath1)
        print('Wrote profile results to %s' % txt_output_fpath2)


# Construct the global profiler.
# The first time it is called, it will be initialized. This is usually a
# NoOpProfiler unless the user requested the real one.  NOTE: kernprof or the
# user may explicitly setup the global profiler.
profile = GlobalProfiler()
