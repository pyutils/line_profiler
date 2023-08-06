#!/usr/bin/env python
import pickle
import functools
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

# NOTE: This needs to be in sync with ../kernprof.py and __init__.py
__version__ = '4.1.0'


def load_ipython_extension(ip):
    """ API for IPython to recognize this module as an IPython extension.
    """
    from .ipython_extension import LineProfilerMagics
    ip.register_magics(LineProfilerMagics)


def is_coroutine(f):
    return inspect.iscoroutinefunction(f)


CO_GENERATOR = 0x0020


def is_generator(f):
    """ Return True if a function is a generator.
    """
    isgen = (f.__code__.co_flags & CO_GENERATOR) != 0
    return isgen


def is_classmethod(f):
    return isinstance(f, classmethod)


class LineProfiler(CLineProfiler):
    """ A profiler that records the execution times of individual lines.
    """

    def __call__(self, func):
        """ Decorate a function to start the profiler on function entry and stop
        it on function exit.
        """
        self.add_function(func)
        if is_classmethod(func):
            wrapper = self.wrap_classmethod(func)
        elif is_coroutine(func):
            wrapper = self.wrap_coroutine(func)
        elif is_generator(func):
            wrapper = self.wrap_generator(func)
        else:
            wrapper = self.wrap_function(func)
        return wrapper

    def wrap_classmethod(self, func):
        """
        Wrap a classmethod to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = func.__func__(func.__class__, *args, **kwds)
            finally:
                self.disable_by_count()
            return result
        return wrapper

    def wrap_coroutine(self, func):
        """
        Wrap a Python 3.5 coroutine to profile it.
        """

        @functools.wraps(func)
        async def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = await func(*args, **kwds)
            finally:
                self.disable_by_count()
            return result

        return wrapper

    def wrap_generator(self, func):
        """ Wrap a generator to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            g = func(*args, **kwds)
            # The first iterate will not be a .send()
            self.enable_by_count()
            try:
                item = next(g)
            except StopIteration:
                return
            finally:
                self.disable_by_count()
            input_ = (yield item)
            # But any following one might be.
            while True:
                self.enable_by_count()
                try:
                    item = g.send(input_)
                except StopIteration:
                    return
                finally:
                    self.disable_by_count()
                input_ = (yield item)
        return wrapper

    def wrap_function(self, func):
        """ Wrap a function to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = func(*args, **kwds)
            finally:
                self.disable_by_count()
            return result
        return wrapper

    def dump_stats(self, filename):
        """ Dump a representation of the data to a file as a pickled LineStats
        object from `get_stats()`.
        """
        lstats = self.get_stats()
        with open(filename, 'wb') as f:
            pickle.dump(lstats, f, pickle.HIGHEST_PROTOCOL)

    def print_stats(self, stream=None, output_unit=None, stripzeros=False,
                    summarize=False, sort=False, rich=0):
        """ Show the gathered statistics.
        """
        lstats = self.get_stats()
        show_text(lstats.timings, lstats.unit, output_unit=output_unit,
                  stream=stream, stripzeros=stripzeros,
                  summarize=summarize, sort=sort, rich=rich)

    def run(self, cmd):
        """ Profile a single executable statment in the main namespace.
        """
        import __main__
        main_dict = __main__.__dict__
        return self.runctx(cmd, main_dict, main_dict)

    def runctx(self, cmd, globals, locals):
        """ Profile a single executable statement in the given namespaces.
        """
        self.enable_by_count()
        try:
            exec(cmd, globals, locals)
        finally:
            self.disable_by_count()
        return self

    def runcall(self, func, *args, **kw):
        """ Profile a single function call.
        """
        self.enable_by_count()
        try:
            return func(*args, **kw)
        finally:
            self.disable_by_count()

    def add_module(self, mod):
        """ Add all the functions in a module and its classes.
        """
        from inspect import isclass, isfunction

        nfuncsadded = 0
        for item in mod.__dict__.values():
            if isclass(item):
                for k, v in item.__dict__.items():
                    if isfunction(v):
                        self.add_function(v)
                        nfuncsadded += 1
            elif isfunction(item):
                self.add_function(item)
                nfuncsadded += 1

        return nfuncsadded


# This could be in the ipython_extension submodule,
# but it doesn't depend on the IPython module so it's easier to just let it stay here.
def is_ipython_kernel_cell(filename):
    """ Return True if a filename corresponds to a Jupyter Notebook cell
    """
    return (
        filename.startswith('<ipython-input-') or
        filename.startswith(os.path.join(tempfile.gettempdir(), 'ipykernel_')) or
        filename.startswith(os.path.join(tempfile.gettempdir(), 'xpython_'))
    )


def show_func(filename, start_lineno, func_name, timings, unit,
              output_unit=None, stream=None, stripzeros=False):
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
        >>> show_func(filename, start_lineno, func_name, timings, unit,
        >>>           output_unit, stream, stripzeros)
    """
    if stream is None:
        stream = sys.stdout

    total_time = sum(t[2] for t in timings)
    if stripzeros and total_time == 0:
        return

    if output_unit is None:
        output_unit = unit
    scalar = unit / output_unit

    linenos = [t[0] for t in timings]

    stream.write('Total time: %g s\n' % (total_time * unit))
    if os.path.exists(filename) or is_ipython_kernel_cell(filename):
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

    ALLOW_SCIENTIFIC_NOTATION = 1
    display = {}

    # Loop over each line to determine better column formatting.
    # Fallback to scientific notation if columns are larger than a threshold.
    for lineno, nhits, time in timings:
        if total_time == 0:  # Happens rarely on empty function
            percent = ''
        else:
            percent = '%5.1f' % (100 * time / total_time)

        time_disp = '%5.1f' % (time * scalar)
        if ALLOW_SCIENTIFIC_NOTATION and len(time_disp) > default_column_sizes['time']:
            time_disp = '%5.1g' % (time * scalar)

        perhit_disp = '%5.1f' % (float(time) * scalar / nhits)
        if ALLOW_SCIENTIFIC_NOTATION and len(perhit_disp) > default_column_sizes['perhit']:
            perhit_disp = '%5.1g' % (float(time) * scalar / nhits)

        nhits_disp = "%d" % nhits
        if ALLOW_SCIENTIFIC_NOTATION and len(nhits_disp) > default_column_sizes['hits']:
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

    # template = '%6s %9s %12s %8s %8s  %-s'
    col_order = ['line', 'hits', 'time', 'perhit', 'percent']
    template = ' '.join(['%' + str(column_sizes[k]) + 's' for k in col_order])
    template = template + '  %-s'

    linenos = range(start_lineno, start_lineno + len(sublines))
    empty = ('', '', '', '')
    header = template % ('Line #', 'Hits', 'Time', 'Per Hit', '% Time',
                         'Line Contents')
    stream.write('\n')
    stream.write(header)
    stream.write('\n')
    stream.write('=' * len(header))
    stream.write('\n')
    for lineno, line in zip(linenos, sublines):
        nhits, time, per_hit, percent = display.get(lineno, empty)
        txt = template % (lineno, nhits, time, per_hit, percent,
                          line.rstrip('\n').rstrip('\r'))
        stream.write(txt)
        stream.write('\n')
    stream.write('\n')


def rich_format_func(filename, start_lineno, func_name, timings, unit,
                     output_unit=None, stream=None, stripzeros=False):
    """
    Show results for a single function -- in style!

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

    Example:
        >>> from line_profiler.line_profiler import rich_format_func
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
        >>> rich_format_func(filename, start_lineno, func_name, timings, unit,
        >>>                  output_unit, stream, stripzeros)
    """
    if stream is None:
        stream = sys.stdout

    total_time = sum(t[2] for t in timings)
    if stripzeros and total_time == 0:
        return

    if output_unit is None:
        output_unit = unit
    scalar = unit / output_unit

    linenos = [t[0] for t in timings]

    stream.write('Total time: %g s\n' % (total_time * unit))
    if os.path.exists(filename) or is_ipython_kernel_cell(filename):
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

    ALLOW_SCIENTIFIC_NOTATION = 1
    display = {}

    # Loop over each line to determine better column formatting.
    # Fallback to scientific notation if columns are larger than a threshold.
    for lineno, nhits, time in timings:
        if total_time == 0:  # Happens rarely on empty function
            percent = ''
        else:
            percent = '%5.1f' % (100 * time / total_time)

        time_disp = '%5.1f' % (time * scalar)
        if ALLOW_SCIENTIFIC_NOTATION and len(time_disp) > default_column_sizes['time']:
            time_disp = '%5.1g' % (time * scalar)

        perhit_disp = '%5.1f' % (float(time) * scalar / nhits)
        if ALLOW_SCIENTIFIC_NOTATION and len(perhit_disp) > default_column_sizes['perhit']:
            perhit_disp = '%5.1g' % (float(time) * scalar / nhits)

        nhits_disp = "%d" % nhits
        if ALLOW_SCIENTIFIC_NOTATION and len(nhits_disp) > default_column_sizes['hits']:
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

    # template = '%6s %9s %12s %8s %8s  %-s'
    col_order = ['line', 'hits', 'time', 'perhit', 'percent']
    lhs_template = ' '.join(['%' + str(column_sizes[k]) + 's' for k in col_order])
    total_template = lhs_template + '  %-s'

    linenos = range(start_lineno, start_lineno + len(sublines))
    empty = ('', '', '', '')
    header = total_template % ('Line #', 'Hits', 'Time', 'Per Hit', '% Time',
                               'Line Contents')
    stream.write('\n')
    stream.write(header)
    stream.write('\n')
    stream.write('=' * len(header))
    stream.write('\n')
    lhs_lines = []
    rhs_lines = []
    for lineno, line in zip(linenos, sublines):
        nhits, time, per_hit, percent = display.get(lineno, empty)
        txt = lhs_template % (lineno, nhits, time, per_hit, percent)

        rhs_lines.append(line.rstrip('\n').rstrip('\r'))
        lhs_lines.append(txt)

    code = '\n'.join(rhs_lines)
    rhs_text = highlight_code(code)
    lhs_text = '\n'.join(lhs_lines)

    # from rich.highlighter import ReprHighlighter
    # from rich.text import Text
    # t = Text(lhs_text)
    # ReprHighlighter().highlight(t)

    # Is there a nicer way to get rich text as ansi?
    # from rich.console import Console
    # import io
    # tmp = io.StringIO()
    # write_console = Console(file=tmp, force_terminal=True)
    # write_console.print(t)
    # lhs_text = tmp.getvalue()
    # lhs_text = highlight_code(lhs_text)
    # print(repr(lhs_text.split('\n')[0]))

    # Does hzcat have a bug with ansi here?
    block = hzcat([lhs_text, rhs_text], sep='  ')
    stream.write(block)

    # stream.write(txt)
    stream.write('\n')
    stream.write('\n')


def show_text(stats, unit, output_unit=None, stream=None, stripzeros=False,
              summarize=False, sort=False, rich=0):
    """ Show text for the given timings.
    """
    if stream is None:
        stream = sys.stdout

    if output_unit is not None:
        stream.write('Timer unit: %g s\n\n' % output_unit)
    else:
        stream.write('Timer unit: %g s\n\n' % unit)

    stats_order = sorted(stats.items())

    if sort:
        # Order by increasing duration
        stats_order = sorted(stats_order, key=lambda kv: sum(t[2] for t in kv[1]))

    for (fn, lineno, name), timings in stats_order:
        if rich:
            rich_format_func(fn, lineno, name, stats[fn, lineno, name], unit,
                             output_unit=output_unit, stream=stream,
                             stripzeros=stripzeros)
        else:
            show_func(fn, lineno, name, stats[fn, lineno, name], unit,
                      output_unit=output_unit, stream=stream,
                      stripzeros=stripzeros)

    if summarize:
        # Summarize the total time for each function

        summary_rows = []
        for (fn, lineno, name), timings in stats_order:
            total_time = sum(t[2] for t in timings) * unit
            summary_rows.append((total_time, fn, lineno, name))

        for total, fn, lineno, name in summary_rows:
            line = '%6.2f seconds - %s:%s - %s\n' % (total, fn, lineno, name)
            stream.write(line)


def load_stats(filename):
    """ Utility function to load a pickled LineStats object from a given
    filename.
    """
    with open(filename, 'rb') as f:
        return pickle.load(f)


def main():
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
    parser.add_argument('profile_output', help='*.lprof file created by kernprof')

    args = parser.parse_args()
    lstats = load_stats(args.profile_output)
    show_text(lstats.timings, lstats.unit, output_unit=args.unit, stripzeros=args.skip_zero)


def hzcat(args, sep=''):
    """
    Horizontally concatenates strings preserving indentation

    Concatenates a list of objects ensuring that the next item in the list is
    all the way to the right of any previous items.

    Args:
        args (List[str]): strings to concatenate
        sep (str, default=''): separator
    """
    import unicodedata
    if '\n' in sep or '\r' in sep:
        raise ValueError('`sep` cannot contain newline characters')

    # TODO: ensure unicode data works correctly for python2
    # args = [unicodedata.normalize('NFC', ensure_unicode(val)) for val in args]
    args = [unicodedata.normalize('NFC', val) for val in args]
    arglines = [a.split('\n') for a in args]
    height = max(map(len, arglines))
    # Do vertical padding
    arglines = [lines + [''] * (height - len(lines)) for lines in arglines]
    # Initialize output
    all_lines = ['' for _ in range(height)]
    width = 0
    n_args = len(args)
    for sx, lines in enumerate(arglines):
        # Concatenate the new string
        for lx, line in enumerate(lines):
            all_lines[lx] += line
        # Find the new maximum horizontal width
        width = max(width, max(map(len, all_lines)))
        if sx < n_args - 1:
            # Horizontal padding on all but last iter
            for lx, line in list(enumerate(all_lines)):
                residual = width - len(line)
                all_lines[lx] = line + (' ' * residual) + sep
            width += len(sep)
    # Clean up trailing whitespace
    all_lines = [line.rstrip(' ') for line in all_lines]
    ret = '\n'.join(all_lines)
    return ret

# import sys
# import os


# Global state that determines if ANSI-coloring text is allowed
# (which is mainly to address non-ANSI compliant windows consoles)
# compliant with https://no-color.org/
NO_COLOR = bool(os.environ.get('NO_COLOR'))  # type: bool


def highlight_code(text, lexer_name='python', **kwargs):
    """
    Highlights a block of text using ANSI tags based on language syntax.

    Args:
        text (str):
            Plain text to parse and highlight

        lexer_name (str):
            Name of language. eg: python, docker, c++.
            For an exhaustive list see :func:`pygments.lexers.get_all_lexers`.
            Defaults to "python".

        **kwargs:
            Passed to pygments.lexers.get_lexer_by_name

    Returns:
        str:
            text - highlighted text if pygments is installed, otherwise the
            plain text is returned unmodified.
    """
    import warnings
    if NO_COLOR:
        return text
    # Resolve extensions to languages
    lexer_name = {
        'py': 'python',
        'h': 'cpp',
        'cpp': 'cpp',
        'cxx': 'cpp',
        'c': 'cpp',
    }.get(lexer_name.replace('.', ''), lexer_name)
    try:

        if sys.platform.startswith('win32'):  # nocover
            # Hack on win32 to support colored output
            try:
                import colorama
                if not colorama.initialise.atexit_done:
                    # Only init if it hasn't been done
                    colorama.init()
            except ImportError:
                warnings.warn(
                    'colorama is not installed, ansi colors may not work')

        import pygments  # type: ignore
        import pygments.lexers  # type: ignore
        import pygments.formatters  # type: ignore
        import pygments.formatters.terminal  # type: ignore

        formatter = pygments.formatters.terminal.TerminalFormatter(bg='dark')
        lexer = pygments.lexers.get_lexer_by_name(lexer_name, **kwargs)
        new_text = pygments.highlight(text, lexer, formatter)

    except ImportError:  # nocover
        warnings.warn('pygments is not installed, code will not be highlighted')
        new_text = text
    return new_text


if __name__ == '__main__':
    main()
