#!/usr/bin/env python
"""
This module defines the core :class:`LineProfiler` class as well as methods to
inspect its output. This depends on the :py:mod:`line_profiler._line_profiler`
Cython backend.
"""
import pickle
import inspect
import linecache
import tempfile
import os
import sys
from argparse import ArgumentError, ArgumentParser

try:
    from ._line_profiler import LineProfiler as CLineProfiler
except ImportError as ex:
    raise ImportError(
        'The line_profiler._line_profiler c-extension is not importable. '
        f'Has it been compiled? Underlying error is ex={ex!r}'
    )
from .profiler_mixin import (ByCountProfilerMixin,
                             is_property, is_cached_property,
                             is_boundmethod, is_classmethod, is_staticmethod,
                             is_partial, is_partialmethod)


# NOTE: This needs to be in sync with ../kernprof.py and __init__.py
__version__ = '4.3.0'

is_function = inspect.isfunction


def load_ipython_extension(ip):
    """ API for IPython to recognize this module as an IPython extension.
    """
    from .ipython_extension import LineProfilerMagics
    ip.register_magics(LineProfilerMagics)


def _get_underlying_functions(func):
    """
    Get the underlying function objects of a callable or an adjacent
    object.

    Returns:
        funcs (list[Callable])
    """
    if any(check(func)
           for check in (is_boundmethod, is_classmethod, is_staticmethod)):
        return _get_underlying_functions(func.__func__)
    if any(check(func)
           for check in (is_partial, is_partialmethod, is_cached_property)):
        return _get_underlying_functions(func.func)
    if is_property(func):
        result = []
        for impl in func.fget, func.fset, func.fdel:
            if impl is not None:
                result.extend(_get_underlying_functions(impl))
        return result
    if not callable(func):
        raise TypeError(f'func = {func!r}: '
                        f'cannot get functions from {type(func)} objects')
    if is_function(func):
        return [func]
    return [type(func).__call__]


class LineProfiler(CLineProfiler, ByCountProfilerMixin):
    """
    A profiler that records the execution times of individual lines.

    This provides the core line-profiler functionality.

    Example:
        >>> import line_profiler
        >>> profile = line_profiler.LineProfiler()
        >>> @profile
        >>> def func():
        >>>     x1 = list(range(10))
        >>>     x2 = list(range(100))
        >>>     x3 = list(range(1000))
        >>> func()
        >>> profile.print_stats()
    """

    def __call__(self, func):
        """
        Decorate a function, method, property, partial object etc. to
        start the profiler on function entry and stop it on function
        exit.
        """
        # Note: if `func` is a `types.FunctionType` which is already
        # decorated by the profiler, the same object is returned;
        # otherwise, wrapper objects are always returned.
        self.add_callable(func)
        return self.wrap_callable(func)

    def add_callable(self, func):
        """
        Register a function, method, property, partial object, etc. with
        the underlying Cython profiler.

        Returns:
            1 if any function is added to the profiler, 0 otherwise
        """
        guard = self._already_wrapped

        nadded = 0
        for impl in _get_underlying_functions(func):
            if guard(impl):
                continue
            self.add_function(impl)
            nadded += 1

        return 1 if nadded else 0

    def dump_stats(self, filename):
        """ Dump a representation of the data to a file as a pickled LineStats
        object from `get_stats()`.
        """
        lstats = self.get_stats()
        with open(filename, 'wb') as f:
            pickle.dump(lstats, f, pickle.HIGHEST_PROTOCOL)

    def print_stats(self, stream=None, output_unit=None, stripzeros=False,
                    details=True, summarize=False, sort=False, rich=False):
        """ Show the gathered statistics.
        """
        lstats = self.get_stats()
        show_text(lstats.timings, lstats.unit, output_unit=output_unit,
                  stream=stream, stripzeros=stripzeros,
                  details=details, summarize=summarize, sort=sort, rich=rich)

    def add_module(self, mod):
        """ Add all the functions in a module and its classes.
        """
        from inspect import isclass

        nfuncsadded = 0
        for item in mod.__dict__.values():
            if isclass(item):
                for k, v in item.__dict__.items():
                    if is_function(v):
                        self.add_function(v)
                        nfuncsadded += 1
            elif is_function(item):
                self.add_function(item)
                nfuncsadded += 1

        return nfuncsadded


# This could be in the ipython_extension submodule,
# but it doesn't depend on the IPython module so it's easier to just let it stay here.
def is_generated_code(filename):
    """ Return True if a filename corresponds to generated code, such as a
    Jupyter Notebook cell.
    """
    filename = os.path.normcase(filename)
    temp_dir = os.path.normcase(tempfile.gettempdir())
    return (
        filename.startswith('<generated') or
        filename.startswith('<ipython-input-') or
        filename.startswith(os.path.join(temp_dir, 'ipykernel_')) or
        filename.startswith(os.path.join(temp_dir, 'xpython_'))
    )


def show_func(filename, start_lineno, func_name, timings, unit,
              output_unit=None, stream=None, stripzeros=False, rich=False):
    """
    Show results for a single function.

    Args:
        filename (str):
            path to the profiled file

        start_lineno (int):
            first line number of profiled function

        func_name (str): name of profiled function

        timings (List[Tuple[int, int, float]]):
            measurements for each line (lineno, nhits, time).

        unit (float):
            The number of seconds used as the cython LineProfiler's unit.

        output_unit (float | None):
            Output unit (in seconds) in which the timing info is displayed.

        stream (io.TextIOBase | None):
            defaults to sys.stdout

        stripzeros (bool):
            if True, prints nothing if the function was not run

        rich (bool):
            if True, attempt to use rich highlighting.

    Example:
        >>> from line_profiler.line_profiler import show_func
        >>> import line_profiler
        >>> # Use a function in this file as an example
        >>> func = line_profiler.line_profiler.show_text
        >>> start_lineno = func.__code__.co_firstlineno
        >>> filename = func.__code__.co_filename
        >>> func_name = func.__name__
        >>> # Build fake timeings for each line in the example function
        >>> import inspect
        >>> num_lines = len(inspect.getsourcelines(func)[0])
        >>> line_numbers = list(range(start_lineno + 3, start_lineno + num_lines))
        >>> timings = [
        >>>     (lineno, idx * 1e13, idx * (2e10 ** (idx % 3)))
        >>>     for idx, lineno in enumerate(line_numbers, start=1)
        >>> ]
        >>> unit = 1.0
        >>> output_unit = 1.0
        >>> stream = None
        >>> stripzeros = False
        >>> rich = 1
        >>> show_func(filename, start_lineno, func_name, timings, unit,
        >>>           output_unit, stream, stripzeros, rich)
    """
    if stream is None:
        stream = sys.stdout

    total_hits = sum(t[1] for t in timings)
    total_time = sum(t[2] for t in timings)

    if stripzeros and total_hits == 0:
        return

    if rich:
        # References:
        # https://github.com/Textualize/rich/discussions/3076
        try:
            from rich.syntax import Syntax
            from rich.highlighter import ReprHighlighter
            from rich.text import Text
            from rich.console import Console
            from rich.table import Table
        except ImportError:
            rich = 0

    if output_unit is None:
        output_unit = unit
    scalar = unit / output_unit

    linenos = [t[0] for t in timings]

    stream.write('Total time: %g s\n' % (total_time * unit))
    if os.path.exists(filename) or is_generated_code(filename):
        stream.write(f'File: {filename}\n')
        stream.write(f'Function: {func_name} at line {start_lineno}\n')
        if os.path.exists(filename):
            # Clear the cache to ensure that we get up-to-date results.
            linecache.clearcache()
        all_lines = linecache.getlines(filename)
        sublines = inspect.getblock(all_lines[start_lineno - 1:])
    else:
        stream.write('\n')
        stream.write(f'Could not find file {filename}\n')
        stream.write('Are you sure you are running this program from the same directory\n')
        stream.write('that you ran the profiler from?\n')
        stream.write("Continuing without the function's contents.\n")
        # Fake empty lines so we can see the timings, if not the code.
        nlines = 1 if not linenos else max(linenos) - min(min(linenos), start_lineno) + 1
        sublines = [''] * nlines

    # Define minimum column sizes so text fits and usually looks consistent
    default_column_sizes = {
        'line': 6,
        'hits': 9,
        'time': 12,
        'perhit': 8,
        'percent': 8,
    }

    display = {}

    # Loop over each line to determine better column formatting.
    # Fallback to scientific notation if columns are larger than a threshold.
    for lineno, nhits, time in timings:
        if total_time == 0:  # Happens rarely on empty function
            percent = ''
        else:
            percent = '%5.1f' % (100 * time / total_time)

        time_disp = '%5.1f' % (time * scalar)
        if len(time_disp) > default_column_sizes['time']:
            time_disp = '%5.3g' % (time * scalar)

        perhit_disp = '%5.1f' % (float(time) * scalar / nhits)
        if len(perhit_disp) > default_column_sizes['perhit']:
            perhit_disp = '%5.3g' % (float(time) * scalar / nhits)

        nhits_disp = "%d" % nhits
        if len(nhits_disp) > default_column_sizes['hits']:
            nhits_disp = '%g' % nhits

        display[lineno] = (nhits_disp, time_disp, perhit_disp, percent)

    # Expand column sizes if the numbers are large.
    column_sizes = default_column_sizes.copy()
    if len(display):
        max_hitlen = max(len(t[0]) for t in display.values())
        max_timelen = max(len(t[1]) for t in display.values())
        max_perhitlen = max(len(t[2]) for t in display.values())
        column_sizes['hits'] = max(column_sizes['hits'], max_hitlen)
        column_sizes['time'] = max(column_sizes['time'], max_timelen)
        column_sizes['perhit'] = max(column_sizes['perhit'], max_perhitlen)

    col_order = ['line', 'hits', 'time', 'perhit', 'percent']
    lhs_template = ' '.join(['%' + str(column_sizes[k]) + 's' for k in col_order])
    template = lhs_template + '  %-s'

    linenos = range(start_lineno, start_lineno + len(sublines))
    empty = ('', '', '', '')
    header = ('Line #', 'Hits', 'Time', 'Per Hit', '% Time', 'Line Contents')
    header = template % header
    stream.write('\n')
    stream.write(header)
    stream.write('\n')
    stream.write('=' * len(header))
    stream.write('\n')

    if rich:
        # Build the RHS and LHS of the table separately
        lhs_lines = []
        rhs_lines = []
        for lineno, line in zip(linenos, sublines):
            nhits, time, per_hit, percent = display.get(lineno, empty)
            txt = lhs_template % (lineno, nhits, time, per_hit, percent)
            rhs_lines.append(line.rstrip('\n').rstrip('\r'))
            lhs_lines.append(txt)

        rhs_text = '\n'.join(rhs_lines)
        lhs_text = '\n'.join(lhs_lines)

        # Highlight the RHS with Python syntax
        rhs = Syntax(rhs_text, 'python', background_color='default')

        # Use default highlights for the LHS
        # TODO: could use colors to draw the eye to longer running lines.
        lhs = Text(lhs_text)
        ReprHighlighter().highlight(lhs)

        # Use a table to horizontally concatenate the text
        # reference: https://github.com/Textualize/rich/discussions/3076
        table = Table(
            box=None,
            padding=0,
            collapse_padding=True,
            show_header=False,
            show_footer=False,
            show_edge=False,
            pad_edge=False,
            expand=False,
        )
        table.add_row(lhs, '  ', rhs)

        # Use a Console to render to the stream
        # Not sure if we should force-terminal or just specify the color system
        # write_console = Console(file=stream, force_terminal=True, soft_wrap=True)
        write_console = Console(file=stream, soft_wrap=True, color_system='standard')
        write_console.print(table)
        stream.write('\n')
    else:
        for lineno, line in zip(linenos, sublines):
            nhits, time, per_hit, percent = display.get(lineno, empty)
            line_ = line.rstrip('\n').rstrip('\r')
            txt = template % (lineno, nhits, time, per_hit, percent, line_)
            try:
                stream.write(txt)
            except UnicodeEncodeError:
                # todo: better handling of windows encoding issue
                # for now just work around it
                line_ = 'UnicodeEncodeError - help wanted for a fix'
                txt = template % (lineno, nhits, time, per_hit, percent, line_)
                stream.write(txt)

            stream.write('\n')
    stream.write('\n')


def show_text(stats, unit, output_unit=None, stream=None, stripzeros=False,
              details=True, summarize=False, sort=False, rich=False):
    """
    Show text for the given timings.

    Ignore:
        # For developer testing, generate some profile output
        python -m kernprof -l -p uuid -m uuid

        # Use this function to view it with rich
        python -m line_profiler -rmtz "uuid.lprof"

        # Use this function to view it without rich
        python -m line_profiler -mtz "uuid.lprof"
    """
    if stream is None:
        stream = sys.stdout

    if output_unit is not None:
        stream.write('Timer unit: %g s\n\n' % output_unit)
    else:
        stream.write('Timer unit: %g s\n\n' % unit)

    if sort:
        # Order by ascending duration
        stats_order = sorted(stats.items(), key=lambda kv: sum(t[2] for t in kv[1]))
    else:
        # Default ordering
        stats_order = sorted(stats.items())

    if details:
        # Show detailed per-line information for each function.
        for (fn, lineno, name), timings in stats_order:
            show_func(fn, lineno, name, stats[fn, lineno, name], unit,
                      output_unit=output_unit, stream=stream,
                      stripzeros=stripzeros, rich=rich)

    if summarize:
        # Summarize the total time for each function
        import functools

        if rich:
            try:
                from rich.console import Console
                from rich.markup import escape
            except ImportError:
                rich = False

        line_template = "{:6.2f} seconds - {}:{} - {}"

        def format_link_fn(fn):
            return f'[link={fn}]{escape(fn)}[/link]'

        if rich:
            printer = Console(file=stream, soft_wrap=True, color_system='standard').print
            format_fn, format_name = format_link_fn, escape
        else:
            printer = functools.partial(print, file=stream)
            format_fn = format_name = str

        for (fn, lineno, name), timings in stats_order:
            total_time = sum(t[2] for t in timings) * unit
            if not stripzeros or total_time:
                line = line_template.format(total_time, format_fn(fn), lineno, format_name(name))
                printer(line)


def load_stats(filename):
    """ Utility function to load a pickled LineStats object from a given
    filename.
    """
    with open(filename, 'rb') as f:
        return pickle.load(f)


def main():
    """
    The line profiler CLI to view output from ``kernprof -l``.
    """
    def positive_float(value):
        val = float(value)
        if val <= 0:
            raise ArgumentError
        return val

    parser = ArgumentParser()
    parser.add_argument('-V', '--version', action='version', version=__version__)
    parser.add_argument(
        '-u',
        '--unit',
        default='1e-6',
        type=positive_float,
        help='Output unit (in seconds) in which the timing info is displayed (default: 1e-6)',
    )
    parser.add_argument(
        '-z',
        '--skip-zero',
        action='store_true',
        help='Hide functions which have not been called',
    )
    parser.add_argument(
        '-r',
        '--rich',
        action='store_true',
        help='Use rich formatting',
    )
    parser.add_argument(
        '-t',
        '--sort',
        action='store_true',
        help='Sort by ascending total time',
    )
    parser.add_argument(
        '-m',
        '--summarize',
        action='store_true',
        help='Print a summary of total function time',
    )
    parser.add_argument('profile_output', help='*.lprof file created by kernprof')

    args = parser.parse_args()
    lstats = load_stats(args.profile_output)
    show_text(
        lstats.timings, lstats.unit, output_unit=args.unit,
        stripzeros=args.skip_zero,
        rich=args.rich,
        sort=args.sort,
        summarize=args.summarize,
    )


if __name__ == '__main__':
    main()
