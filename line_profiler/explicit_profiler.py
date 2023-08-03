"""
The idea is that we are going to expose a top-level ``profile`` decorator which
will be disabled by default **unless** you are running with with line profiler
itself OR if the LINE_PROFILE environment variable is True.

This uses the :mod:`atexit` module to perform a profile dump at the end.

This work is ported from :mod:`xdev`.

Basic usage is to import line_profiler and decorate your function with
line_profiler.profile.  By default this does nothing, it's a no-op decorator.
However, if you run with the environment variable ``LINE_PROFILER=1`` or if
``'--profile' in sys.argv'``, then it enables profiling and at the end of your
script it will output the profile text.

Here is a minimal example:

.. code:: bash

    # Write demo python script to disk
    python -c "if 1:
        import textwrap
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
        with open('demo.py', 'w') as file:
            file.write(text)
    "

    echo "---"
    echo "## Base Case: Run without any profiling"
    python demo.py

    echo "---"
    echo "## Option 0: Original Usage"
    python -m kernprof -l demo.py
    python -m line_profiler -rmt demo.py.lprof

    echo "---"
    echo "## Option 1: Enable profiler with the command line"
    python demo.py --line-profile

    echo "---"
    echo "## Option 1: Enable profiler with an environment variable"
    LINE_PROFILE=1 python demo.py


An example with in-code enabling:

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


An example with in-code enabling and disabling:

.. code:: bash

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


_FALSY_STRINGS = {'', '0', 'off', 'false', 'no'}


class GlobalProfiler:
    """
    Manages a profiler that will output on interpreter exit.

    Attributes:
        setup_config (Dict[str, List[str]]):
            Determines how the implicit setup behaves by defining which
            environment variables / command line flags to look for.

        output_prefix (str):
            The prefix of any output files written. Should include
            a part of a filename. Defaults to "profile_output".

        write_config (Dict[str, bool]):
            Which outputs are enabled. All default to True.
            Options are lprof, text, timestamped_text, and stdout.

        show_config (Dict[str, bool]):
            Display configuration options. Some outputs force certain options.
            (e.g. text always has details and is never rich).

        enabled (bool | None):
            True if the profiler is enabled (i.e. if it will wrap a function
            that it decorates with a real profiler). If None, then the value
            defaults based on the ``setup_config``, :py:obj:`os.environ`, and
            :py:obj:`sys.argv`.

    Example:
        >>> from line_profiler.explicit_profiler import *  # NOQA
        >>> self = GlobalProfiler()
        >>> # Setting the _profile attribute prevents atexit from running.
        >>> self._profile = LineProfiler()
        >>> # User can personalize the configuration
        >>> self.show_config['details'] = True
        >>> self.write_config['lprof'] = False
        >>> self.write_config['text'] = False
        >>> self.write_config['timestamped_text'] = False
        >>> # Demo data: a function to profile
        >>> def foo():
        >>>     n = 100
        >>>     for i in range(a + b):
        >>>         if n % 2 == 0:
        >>>             n = n // 2
        >>>         else:
        >>>             n = 3 * n + 1
        >>>     return n
        >>> # Disabled by default, implicitly checks to auto-enable on first wrap
        >>> assert self.enabled is None
        >>> foo_wrap = self(foo)
        >>> assert self.enabled is False
        >>> assert foo_wrap is foo
        >>> # Can explicitly enable
        >>> self.enable()
        >>> foo_wrap = self(foo)
        >>> assert self.enabled is True
        >>> assert foo_wrap is not foo
        >>> foo_wrap()
        >>> # Can explicitly request output
        >>> self.show()
    """

    def __init__(self):
        self.setup_config = {
            'environ_flags': ['LINE_PROFILE'],
            'cli_flags': ['--line-profile', '--line_profile'],
        }
        self.output_prefix = 'profile_output'
        self._profile = None
        self.enabled = None

        # Control which outputs will be written on exit
        self.write_config = {
            'lprof': True,
            'text': True,
            'timestamped_text': True,
            'stdout': True,
        }

        # Configuration for how output will be displayed
        self.show_config = {
            'sort': 1,
            'stripzeros': 1,
            'rich': 1,
            'details': 0,
            'summarize': 1,
        }

    def _kernprof_overwrite(self, profile):
        """
        Kernprof will call this when it runs, so we can use its profile object
        instead of our own. Note: when kernprof overwrites us we wont register
        an atexit hook. This is what we want because kernprof wants us to use
        another program to read its output file.
        """
        self._profile = profile
        self.enabled = True

    def _implicit_setup(self):
        """
        Called once the first time the user decorates a function with
        ``line_profiler.profile`` and they have not explicitly setup the global
        profiling options.
        """
        environ_flags = self.setup_config['environ_flags']
        cli_flags = self.setup_config['cli_flags']
        is_profiling = any(os.environ.get(f, '').lower() not in _FALSY_STRINGS
                           for f in environ_flags)
        is_profiling |= any(f in sys.argv for f in cli_flags)
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
        Explicitly initialize and disable this global profiler.
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
        Write the managed profiler stats to enabled outputs.

        If the implicit setup triggered, then this will be called by atexit.
        """
        import io
        import pathlib

        srite_stdout = self.write_config['stdout']
        write_text = self.write_config['text']
        write_timestamped_text = self.write_config['timestamped_text']
        write_lprof = self.write_config['lprof']

        if srite_stdout:
            kwargs = self.show_config.copy()
            self._profile.print_stats(**kwargs)

        if write_text or write_timestamped_text:
            stream = io.StringIO()
            # Text output always contains details, and cannot be rich.
            text_kwargs = self.show_config.copy()
            text_kwargs['rich'] = 0
            text_kwargs['details'] = 1
            self._profile.print_stats(stream=stream, **text_kwargs)
            raw_text = stream.getvalue()

            if write_text:
                txt_output_fpath1 = pathlib.Path(f'{self.output_prefix}.txt')
                txt_output_fpath1.write_text(raw_text)
                print('Wrote profile results to %s' % txt_output_fpath1)

            if write_timestamped_text:
                from datetime import datetime as datetime_cls
                now = datetime_cls.now()
                timestamp = now.strftime('%Y-%m-%dT%H%M%S')
                txt_output_fpath2 = pathlib.Path(f'{self.output_prefix}_{timestamp}.txt')
                txt_output_fpath2.write_text(raw_text)
                print('Wrote profile results to %s' % txt_output_fpath2)

        if write_lprof:
            lprof_output_fpath = pathlib.Path(f'{self.output_prefix}.lprof')
            self._profile.dump_stats(lprof_output_fpath)
            print('Wrote profile results to %s' % lprof_output_fpath)
            print('To view details run:')
            print(sys.executable + ' -m line_profiler -rtmz ' + str(lprof_output_fpath))


# Construct the global profiler.
# The first time it is called, it will be initialized. This is usually a
# NoOpProfiler unless the user requested the real one.
# NOTE: kernprof or the user may explicitly setup the global profiler.
profile = GlobalProfiler()