#!/usr/bin/env python
"""
Script to conveniently run profilers on code in a variety of
circumstances.

To profile a script, decorate the functions of interest with
:py:deco:`profile <line_profiler.explicit_profiler.GlobalProfiler>`:

.. code:: bash

    echo "if 1:
        @profile
        def main():
            1 + 1
        main()
    " > script_to_profile.py

NOTE:

    New in 4.1.0: Instead of relying on injecting :py:deco:`profile`
    into the builtins you can now ``import line_profiler`` and use
    :py:deco:`line_profiler.profile <line_profiler.explicit_profiler.GlobalProfiler>`
    to decorate your functions.  This allows the script to remain
    functional even if it is not actively profiled.  See
    :py:mod:`!line_profiler` (:ref:`link <line-profiler-basic-usage>`) for
    details.


Then run the script using :program:`kernprof`:

.. code:: bash

    kernprof -b script_to_profile.py

By default this runs with the default :py:mod:`cProfile` profiler and
does not require compiled modules. Instructions to view the results will
be given in the output. Alternatively, adding :option:`!-v` to the
command line will write results to stdout.

To enable line-by-line profiling, :py:mod:`line_profiler` must be
available and compiled, and the :option:`!-l` argument should be added to
the :program:`kernprof` invocation:

.. code:: bash

    kernprof -lb script_to_profile.py

NOTE:

    New in 4.3.0: More code execution options are added:

    * :command:`kernprof <options> -m some.module <args to module>`
      parallels :command:`python -m` and runs the provided module as
      :py:mod:`__main__`.
    * :command:`kernprof <options> -c "some code" <args to code>`
      parallels :command:`python -c` and executes the provided literal
      code.
    * :command:`kernprof <options> - <args to code>` parallels
      :command:`python -` and executes literal code passed via the
      :file:`stdin`.

    See also
    :doc:`kernprof invocations </manual/examples/example_kernprof>`.

For more details and options, refer to the CLI help.
To view the :program:`kernprof` help text run:

.. code:: bash

    kernprof --help

which displays:

.. code::

    usage: kernprof [-h] [-V] [-l] [-b] [-o OUTFILE] [-s SETUP] [-v] [-q] [-r] [-u UNIT] [-z] [-i [OUTPUT_INTERVAL]] [-p {path/to/script | object.dotted.path}[,...]]
                    [--no-preimports] [--prof-imports]
                    {path/to/script | -m path.to.module | -c "literal code"} ...

    Run and profile a python script.

    positional arguments:
      {path/to/script | -m path.to.module | -c "literal code"}
                            The python script file, module, or literal code to run
      args                  Optional script arguments

    options:
      -h, --help            show this help message and exit
      -V, --version         show program's version number and exit
      -l, --line-by-line    Use the line-by-line profiler instead of cProfile. Implies --builtin.
      -b, --builtin         Put 'profile' in the builtins. Use 'profile.enable()'/'.disable()', '@profile' to decorate functions, or 'with profile:' to profile a section of code.
      -o, --outfile OUTFILE
                            Save stats to <outfile> (default: 'scriptname.lprof' with --line-by-line, 'scriptname.prof' without)
      -s, --setup SETUP     Code to execute before the code to profile
      -v, --verbose, --view
                            Increase verbosity level. At level 1, view the profiling results in addition to saving them; at level 2, show other diagnostic info.
      -q, --quiet           Decrease verbosity level. At level -1, disable helpful messages (e.g. "Wrote profile results to <...>"); at level -2, silence the stdout; at level -3,
                            silence the stderr.
      -r, --rich            Use rich formatting if viewing output
      -u, --unit UNIT       Output unit (in seconds) in which the timing info is displayed (default: 1e-6)
      -z, --skip-zero       Hide functions which have not been called
      -i, --output-interval [OUTPUT_INTERVAL]
                            Enables outputting of cumulative profiling results to file every n seconds. Uses the threading module. Minimum value is 1 (second). Defaults to
                            disabled.
      -p, --prof-mod {path/to/script | object.dotted.path}[,...]
                            List of modules, functions and/or classes to profile specified by their name or path. These profiling targets can be supplied both as comma-separated
                            items, or separately with multiple copies of this flag. Packages are automatically recursed into unless they are specified with `<pkg>.__init__`. Adding
                            the current script/module profiles the entirety of it. Only works with line_profiler -l, --line-by-line.
      --no-preimports       Instead of eagerly importing all profiling targets specified via -p and profiling them, only profile those that are directly imported in the profiled
                            code. Only works with line_profiler -l, --line-by-line.
      --prof-imports        If specified, modules specified to `--prof-mod` will also autoprofile modules that they import. Only works with line_profiler -l, --line-by-line

NOTE:

    New in 4.3.0: For more intuitive profiling behavior, profiling
    targets in :option:`!--prof-mod` (except the profiled script/code)
    are now:

    * Eagerly pre-imported to be profiled (see
      :py:mod:`line_profiler.autoprofile.eager_preimports`),
      regardless of whether those imports directly occur in the profiled
      script/module/code.
    * Descended/Recursed into if they are packages; pass
      ``<pkg_name>.__init__`` instead of ``<pkg_name>`` to curtail
      descent and limit profiling to classes and functions in the local
      namespace of the :file:`__init__.py`.

    To restore the old behavior, pass the :option:`!--no-preimports`
    flag.
"""
import atexit
import builtins
import functools
import os
import sys
import threading
import asyncio  # NOQA
import concurrent.futures  # NOQA
import contextlib
import shutil
import tempfile
import time
import warnings
from argparse import ArgumentError, ArgumentParser
from io import StringIO
from operator import methodcaller
from runpy import run_module
from pathlib import Path
from pprint import pformat
from shlex import quote
from textwrap import indent, dedent
from types import MethodType, SimpleNamespace

# NOTE: This version needs to be manually maintained in
# line_profiler/line_profiler.py and line_profiler/__init__.py as well
__version__ = '4.3.0'

# Guard the import of cProfile such that 3.x people
# without lsprof can still use this script.
try:
    from cProfile import Profile
except ImportError:
    from profile import Profile  # type: ignore[assignment,no-redef]

from line_profiler.profiler_mixin import ByCountProfilerMixin
from line_profiler._logger import Logger
from line_profiler import _diagnostics as diagnostics


DIAGNOSITICS_VERBOSITY = 2


def execfile(filename, globals=None, locals=None):
    """ Python 3.x doesn't have :py:func:`execfile` builtin """
    with open(filename, 'rb') as f:
        exec(compile(f.read(), filename, 'exec'), globals, locals)
# =====================================


class ContextualProfile(ByCountProfilerMixin, Profile):
    """ A subclass of :py:class:`Profile` that adds a context manager
    for Python 2.5 with: statements and a decorator.
    """
    def __init__(self, *args, **kwds):
        super(ByCountProfilerMixin, self).__init__(*args, **kwds)
        self.enable_count = 0

    def __call__(self, func):
        return self.wrap_callable(func)

    def enable_by_count(self, subcalls=True, builtins=True):
        """ Enable the profiler if it hasn't been enabled before.
        """
        if self.enable_count == 0:
            self.enable(subcalls=subcalls, builtins=builtins)
        self.enable_count += 1

    def disable_by_count(self):
        """ Disable the profiler if the number of disable requests matches the
        number of enable requests.
        """
        if self.enable_count > 0:
            self.enable_count -= 1
            if self.enable_count == 0:
                self.disable()

    # FIXME: `profile.Profile` is fundamentally incompatible with the
    # by-count paradigm we use, as it can't be `.enable()`-ed nor
    # `.disable()`-ed


class RepeatedTimer:
    """
    Background timer for outputting file every ``n`` seconds.

    Adapted from [SO474528]_.

    References:
        .. [SO474528] https://stackoverflow.com/questions/474528/execute-function-every-x-seconds/40965385#40965385
    """
    def __init__(self, interval, dump_func, outfile):
        self._timer = None
        self.interval = interval
        self.dump_func = dump_func
        self.outfile = outfile
        self.is_running = False
        self.next_call = time.time()
        self.start()

    def _run(self):
        self.is_running = False
        self.start()
        self.dump_func(self.outfile)

    def start(self):
        if not self.is_running:
            self.next_call += self.interval
            self._timer = threading.Timer(self.next_call - time.time(), self._run)
            self._timer.start()
            self.is_running = True

    def stop(self):
        self._timer.cancel()
        self.is_running = False


def find_module_script(module_name):
    """Find the path to the executable script for a module or package."""
    from line_profiler.autoprofile.util_static import modname_to_modpath

    for suffix in '.__main__', '':
        fname = modname_to_modpath(module_name + suffix)
        if fname:
            return fname

    sys.stderr.write('Could not find module %s\n' % module_name)
    raise SystemExit(1)


def find_script(script_name, exit_on_error=True):
    """ Find the script.

    If the input is not a file, then :envvar:`PATH` will be searched.
    """
    if os.path.isfile(script_name):
        return script_name
    path = os.getenv('PATH', os.defpath).split(os.pathsep)
    for dir in path:
        if dir == '':
            continue
        fn = os.path.join(dir, script_name)
        if os.path.isfile(fn):
            return fn

    msg = f'Could not find script {script_name!r}'
    if exit_on_error:
        print(msg, file=sys.stderr)
        raise SystemExit(1)
    else:
        raise FileNotFoundError(msg)


def _python_command():
    """
    Return a command that corresponds to :py:data:`sys.executable`.
    """
    for abbr in 'python', 'python3':
        if os.path.samefile(shutil.which(abbr), sys.executable):
            return abbr
    return sys.executable


def _normalize_profiling_targets(targets):
    """
    Normalize the parsed :option:`!--prof-mod` by:

    * Normalizing file paths with :py:func:`find_script()`, and
      subsequently to absolute paths.
    * Splitting non-file paths at commas into (presumably) file paths
      and/or dotted paths.
    * Removing duplicates.
    """
    def find(path):
        try:
            path = find_script(path, exit_on_error=False)
        except FileNotFoundError:
            return None
        return os.path.abspath(path)

    results = {}
    for chunk in targets:
        filename = find(chunk)
        if filename is not None:
            results.setdefault(filename)
            continue
        for subchunk in chunk.split(','):
            filename = find(subchunk)
            results.setdefault(subchunk if filename is None else filename)
    return list(results)


class _restore:
    """
    Restore a collection like :py:data:`sys.path` after running code
    which potentially modifies it.
    """
    def __init__(self, obj, getter, setter):
        self.obj = obj
        self.setter = setter
        self.getter = getter
        self.old = None

    def __enter__(self):
        assert self.old is None
        self.old = self.getter(self.obj)

    def __exit__(self, *_, **__):
        self.setter(self.obj, self.old)
        self.old = None

    def __call__(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)

        return wrapper

    @classmethod
    def sequence(cls, seq):
        """
        Example
        -------
        >>> l = [1, 2, 3]
        >>>
        >>> with _restore.sequence(l):
        ...     print(l)
        ...     l.append(4)
        ...     print(l)
        ...     l[:] = 5, 6
        ...     print(l)
        ...
        [1, 2, 3]
        [1, 2, 3, 4]
        [5, 6]
        >>> l
        [1, 2, 3]
        """
        def set_list(orig, copy):
            orig[:] = copy

        return cls(seq, methodcaller('copy'), set_list)

    @classmethod
    def mapping(cls, mpg):
        """
        Example
        -------
        >>> d = {1: 2}
        >>>
        >>> with _restore.mapping(d):
        ...     print(d)
        ...     d[2] = 3
        ...     print(d)
        ...     d.clear()
        ...     d.update({1: 4, 3: 5})
        ...     print(d)
        ...
        {1: 2}
        {1: 2, 2: 3}
        {1: 4, 3: 5}
        >>> d
        {1: 2}
        """
        def set_mapping(orig, copy):
            orig.clear()
            orig.update(copy)

        return cls(mpg, methodcaller('copy'), set_mapping)

    @classmethod
    def instance_dict(cls, obj):
        """
        Example
        -------
        >>> class Obj:
        ...     def __init__(self, x, y):
        ...         self.x, self.y = x, y
        ...
        ...     def __repr__(self):
        ...         return 'Obj({0.x!r}, {0.y!r})'.format(self)
        ...
        >>>
        >>> obj = Obj(1, 2)
        >>>
        >>> with _restore.instance_dict(obj):
        ...     print(obj)
        ...     obj.x, obj.y, obj.z = 4, 5, 6
        ...     print(obj, obj.z)
        ...
        Obj(1, 2)
        Obj(4, 5) 6
        >>> obj
        Obj(1, 2)
        >>> hasattr(obj, 'z')
        False
        """
        return cls.mapping(vars(obj))


def pre_parse_single_arg_directive(args, flag, sep='--'):
    """
    Pre-parse high-priority single-argument directives like
    :option:`!-m module` to emulate the behavior of
    :command:`python [...]`.

    Examples
    --------
    >>> import functools
    >>> pre_parse = functools.partial(pre_parse_single_arg_directive,
    ...                               flag='-m')

    Normal parsing:

    >>> pre_parse(['foo', 'bar', 'baz'])
    (['foo', 'bar', 'baz'], None, [])
    >>> pre_parse(['foo', 'bar', '-m', 'baz'])
    (['foo', 'bar'], 'baz', [])
    >>> pre_parse(['foo', 'bar', '-m', 'baz', 'foobar'])
    (['foo', 'bar'], 'baz', ['foobar'])

    Erroneous case:

    >>> pre_parse(['foo', 'bar', '-m'])
    Traceback (most recent call last):
      ...
    ValueError: argument expected for the -m option

    Prevent erroneous consumption of the flag by passing it `'--'`:

    >>> pre_parse(['foo', '--', 'bar', '-m', 'baz'])
    (['foo', '--'], None, ['bar', '-m', 'baz'])
    >>> pre_parse(['foo', '-m', 'spam',
    ...            'eggs', '--', 'bar', '-m', 'baz'])
    (['foo'], 'spam', ['eggs', '--', 'bar', '-m', 'baz'])
    """
    args = list(args)
    pre = []
    post = []
    try:
        i_sep = args.index(sep)
    except ValueError:  # No such element
        pass
    else:
        pre[:] = args[:i_sep]
        post[:] = args[i_sep + 1:]
        pre_pre, arg, pre_post = pre_parse_single_arg_directive(pre, flag)
        if arg is None:
            assert not pre_post
            return pre_pre + [sep], arg, post
        else:
            return pre_pre, arg, [*pre_post, sep, *post]
    try:
        i_flag = args.index(flag)
    except ValueError:  # No such element
        return args, None, []
    if i_flag == len(args) - 1:  # Last element
        raise ValueError(f'argument expected for the {flag} option')
    return args[:i_flag], args[i_flag + 1], args[i_flag + 2:]


@_restore.sequence(sys.argv)
@_restore.sequence(sys.path)
@_restore.instance_dict(diagnostics)
def main(args=None):
    """
    Runs the command line interface

    Note:
        To help with traceback formatting, the deletion of temporary
        files created during execution may be deferred to when the
        interpreter exits.
    """
    def positive_float(value):
        val = float(value)
        if val <= 0:
            raise ArgumentError
        return val

    def no_op(*_, **__) -> None:
        pass

    parser_kwargs = {
        'description': 'Run and profile a python script.',
    }

    if args is None:
        args = sys.argv[1:]

    # Special cases: `kernprof [...] -m <module>` or
    # `kernprof [...] -c <script>` should terminate the parsing of all
    # subsequent options
    if '-m' in args and '-c' in args:
        special_mode = min(['-c', '-m'], key=args.index)
    elif '-m' in args:
        special_mode = '-m'
    else:
        special_mode = '-c'
    args, thing, post_args = pre_parse_single_arg_directive(args, special_mode)
    if special_mode == '-m':
        module, literal_code = thing, None
    else:
        module, literal_code = None, thing

    if module is literal_code is None:  # Normal execution
        real_parser, = parsers = [ArgumentParser(**parser_kwargs)]
        help_parser = None
    else:
        # We've already consumed the `-m <module>`, so we need a dummy
        # parser for generating the help text;
        # but the real parser should not consume the `options.script`
        # positional arg, and it it got the `--help` option, it should
        # hand off the the dummy parser
        real_parser = ArgumentParser(add_help=False, **parser_kwargs)
        real_parser.add_argument('-h', '--help', action='store_true')
        help_parser = ArgumentParser(**parser_kwargs)
        parsers = [real_parser, help_parser]
    for parser in parsers:
        parser.add_argument('-V', '--version', action='version', version=__version__)
        parser.add_argument('-l', '--line-by-line', action='store_true',
                            help='Use the line-by-line profiler instead of cProfile. Implies --builtin.')
        parser.add_argument('-b', '--builtin', action='store_true',
                            help="Put 'profile' in the builtins. Use 'profile.enable()'/'.disable()', "
                            "'@profile' to decorate functions, or 'with profile:' to profile a "
                            'section of code.')
        parser.add_argument('-o', '--outfile',
                            help="Save stats to <outfile> (default: 'scriptname.lprof' with "
                            "--line-by-line, 'scriptname.prof' without)")
        parser.add_argument('-s', '--setup',
                            help='Code to execute before the code to profile')
        parser.add_argument('-v', '--verbose', '--view',
                            action='count', default=0,
                            help='Increase verbosity level. '
                            'At level 1, view the profiling results '
                            'in addition to saving them; '
                            'at level 2, show other diagnostic info.')
        parser.add_argument('-q', '--quiet',
                            action='count', default=0,
                            help='Decrease verbosity level. '
                            'At level -1, disable helpful messages '
                            '(e.g. "Wrote profile results to <...>"); '
                            'at level -2, silence the stdout; '
                            'at level -3, silence the stderr.')
        parser.add_argument('-r', '--rich', action='store_true',
                            help='Use rich formatting if viewing output')
        parser.add_argument('-u', '--unit', default='1e-6', type=positive_float,
                            help='Output unit (in seconds) in which the timing info is '
                            'displayed (default: 1e-6)')
        parser.add_argument('-z', '--skip-zero', action='store_true',
                            help="Hide functions which have not been called")
        parser.add_argument('-i', '--output-interval', type=int, default=0, const=0, nargs='?',
                            help="Enables outputting of cumulative profiling results to file every n seconds. Uses the threading module. "
                            "Minimum value is 1 (second). Defaults to disabled.")
        parser.add_argument('-p', '--prof-mod',
                            action='append',
                            metavar=("{path/to/script | object.dotted.path}"
                                     "[,...]"),
                            help="List of modules, functions and/or classes "
                            "to profile specified by their name or path. "
                            "These profiling targets can be supplied both as "
                            "comma-separated items, or separately with "
                            "multiple copies of this flag. "
                            "Packages are automatically recursed into unless "
                            "they are specified with `<pkg>.__init__`. "
                            "Adding the current script/module profiles the "
                            "entirety of it. "
                            "Only works with line_profiler -l, --line-by-line.")
        parser.add_argument('--no-preimports',
                            action='store_true',
                            help="Instead of eagerly importing all profiling "
                            "targets specified via -p and profiling them, "
                            "only profile those that are directly imported in "
                            "the profiled code. "
                            "Only works with line_profiler -l, --line-by-line.")
        parser.add_argument('--prof-imports', action='store_true',
                            help="If specified, modules specified to `--prof-mod` will also autoprofile modules that they import. "
                            "Only works with line_profiler -l, --line-by-line")

        if parser is help_parser or module is literal_code is None:
            parser.add_argument('script',
                                metavar='{path/to/script'
                                ' | -m path.to.module | -c "literal code"}',
                                help='The python script file, module, or '
                                'literal code to run')
        parser.add_argument('args', nargs='...', help='Optional script arguments')

    # Hand off to the dummy parser if necessary to generate the help
    # text
    options = SimpleNamespace(**vars(real_parser.parse_args(args)))
    # TODO: make flags later where appropriate
    options.dryrun = diagnostics.NO_EXEC
    options.static = diagnostics.STATIC_ANALYSIS
    if help_parser and getattr(options, 'help', False):
        help_parser.print_help()
        exit()
    try:
        del options.help
    except AttributeError:
        pass
    # Add in the pre-partitioned arguments cut off by `-m <module>` or
    # `-c <script>`
    options.args += post_args
    if module is not None:
        options.script = module

    tempfile_source_and_content = None
    if literal_code is not None:
        tempfile_source_and_content = 'command', literal_code
    elif options.script == '-' and not module:
        tempfile_source_and_content = 'stdin', sys.stdin.read()

    # Handle output
    options.verbose -= options.quiet
    options.debug = (diagnostics.DEBUG
                     or options.verbose >= DIAGNOSITICS_VERBOSITY)
    logger_kwargs = {'name': 'kernprof'}
    logger_kwargs['backend'] = 'auto'
    if options.debug:
        # Debugging forces the stdlib logger
        logger_kwargs['verbose'] = 2
        logger_kwargs['backend'] = 'stdlib'
    elif options.verbose > -1:
        logger_kwargs['verbose'] = 1
    else:
        logger_kwargs['verbose'] = 0
    logger_kwargs['stream'] = {
        'format': '[%(name)s %(asctime)s %(levelname)s] %(message)s',
    }
    # Reinitialize the diagnostic logs, we are very likely the main script.
    diagnostics.log = Logger(**logger_kwargs)
    if options.rich:
        try:
            import rich  # noqa: F401
        except ImportError:
            options.rich = False
            diagnostics.log.debug('`rich` not installed, unsetting --rich')

    if module is not None:
        diagnostics.log.debug(f'Profiling module: {module}')
    elif tempfile_source_and_content:
        diagnostics.log.debug(
            f'Profiling script read from: {tempfile_source_and_content[0]}')
    else:
        diagnostics.log.debug(f'Profiling script: {options.script}')

    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        if options.verbose < -1:  # Suppress stdout
            devnull = enter(open(os.devnull, mode='w'))
            enter(contextlib.redirect_stdout(devnull))
        if options.verbose < -2:  # Suppress stderr
            enter(contextlib.redirect_stderr(devnull))
        # Instead of relying on `tempfile.TemporaryDirectory`, manually
        # manage a tempdir to ensure that files exist at
        # traceback-formatting time if needs be
        options.tmpdir = tmpdir = tempfile.mkdtemp()
        if diagnostics.KEEP_TEMPDIRS:
            cleanup = no_op
        else:
            cleanup = functools.partial(
                _remove, tmpdir, recursive=True, missing_ok=True,
            )
        if tempfile_source_and_content:
            try:
                _write_tempfile(*tempfile_source_and_content, options)
            except Exception:
                # Tempfile creation failed, delete the tempdir ASAP
                cleanup()
                raise
        try:
            _main(options, module)
        except BaseException:
            # Defer deletion to after the traceback has been formatted
            # if needs be
            if os.listdir(tmpdir):
                atexit.register(cleanup)
            else:  # Empty tempdir, just delete it
                cleanup()
            raise
        else:  # Execution succeeded, delete the tempdir ASAP
            cleanup()


def _touch_tempfile(*args, **kwargs):
    """
    Wrapper around :py:func:`tempfile.mkstemp()` which drops and closes
    the integer handle (which we don't need and may cause issues on some
    platforms).
    """
    handle, path = tempfile.mkstemp(*args, **kwargs)
    try:
        os.close(handle)
    except Exception:
        os.remove(path)
        raise
    return path


def _write_tempfile(source, content, options):
    """
    Called by :py:func:`main()` to handle :command:`kernprof -c` and
    :command:`kernprof -`;
    not to be invoked on its own.
    """
    # Set up the script to be run
    file_prefix = f'kernprof-{source}'
    # Do what 3.14 does (#103998)... and also just to be user-friendly
    content = dedent(content)
    fname = os.path.join(options.tmpdir, file_prefix + '.py')
    with open(fname, mode='w') as fobj:
        print(content, file=fobj)
    diagnostics.log.debug(f'Wrote temporary script file to {fname!r}:')
    options.script = fname
    # Add the tempfile to `--prof-mod`
    if options.prof_mod:
        options.prof_mod.append(fname)
    else:
        options.prof_mod = [fname]
    # Set the output file to somewhere nicer (also take care of possible
    # filename clash)
    if not options.outfile:
        extension = 'lprof' if options.line_by_line else 'prof'
        options.outfile = _touch_tempfile(dir=os.curdir,
                                          prefix=file_prefix + '-',
                                          suffix='.' + extension)
        diagnostics.log.debug(
            f'Using default output destination {options.outfile!r}')


def _write_preimports(prof, options, exclude):
    """
    Called by :py:func:`main()` to handle eager pre-imports;
    not to be invoked on its own.
    """
    from line_profiler.autoprofile.eager_preimports import (
        is_dotted_path, write_eager_import_module)
    from line_profiler.autoprofile.util_static import modpath_to_modname
    from line_profiler.autoprofile.autoprofile import (
        _extend_line_profiler_for_profiling_imports as upgrade_profiler)

    filtered_targets = []
    recurse_targets = []
    invalid_targets = []
    for target in options.prof_mod:
        if is_dotted_path(target):
            modname = target
        else:
            # Paths already normalized by
            # `_normalize_profiling_targets()`
            if not os.path.exists(target):
                invalid_targets.append(target)
                continue
            if any(os.path.samefile(target, excluded) for excluded in exclude):
                # Ignore the script to be run in eager importing
                # (`line_profiler.autoprofile.autoprofile.run()` will
                # handle it)
                continue
            modname = modpath_to_modname(target, hide_init=False)
        if modname is None:  # Not import-able
            invalid_targets.append(target)
            continue
        if modname.endswith('.__init__'):
            modname = modname.rpartition('.')[0]
            targets = filtered_targets
        else:
            targets = recurse_targets
        targets.append(modname)
    if invalid_targets:
        invalid_targets = sorted(set(invalid_targets))
        msg = ('{} profile-on-import target{} cannot be converted to '
               'dotted-path form: {!r}'
               .format(len(invalid_targets),
                       '' if len(invalid_targets) == 1 else 's',
                       invalid_targets))
        warnings.warn(msg)
        diagnostics.log.warn(msg)
    if not (filtered_targets or recurse_targets):
        return
    # We could've done everything in-memory with `io.StringIO` and
    # `exec()`, but that results in indecipherable tracebacks should
    # anything goes wrong;
    # so we write to a tempfile and `execfile()` it
    upgrade_profiler(prof)
    temp_mod_path = _touch_tempfile(dir=options.tmpdir,
                                    prefix='kernprof-eager-preimports-',
                                    suffix='.py')
    write_module_kwargs = {
        'dotted_paths': filtered_targets,
        'recurse': recurse_targets,
        'static': options.static,
    }
    temp_file = open(temp_mod_path, mode='w')
    if options.debug:
        with StringIO() as sio:
            write_eager_import_module(stream=sio, **write_module_kwargs)
            code = sio.getvalue()
        with temp_file as fobj:
            print(code, file=fobj)
        diagnostics.log.debug(
            'Wrote temporary module for pre-imports '
            f'to {temp_mod_path!r}:')
    else:
        with temp_file as fobj:
            write_eager_import_module(stream=fobj, **write_module_kwargs)
    if not options.dryrun:
        ns = {}  # Use a fresh namespace
        execfile(temp_mod_path, ns, ns)
    # Delete the tempfile ASAP if its execution succeeded
    if diagnostics.KEEP_TEMPDIRS:
        diagnostics.log.debug('Keep temporary preimport path: {temp_mod_path}')
    else:
        _remove(temp_mod_path)


def _remove(path, *, recursive=False, missing_ok=False):
    path = Path(path)
    if path.is_dir():
        if recursive:
            shutil.rmtree(path, ignore_errors=missing_ok)
        else:
            path.rmdir()
    else:
        path.unlink(missing_ok=missing_ok)


def _dump_filtered_stats(tmpdir, prof, filename):
    import os
    import pickle

    # Build list of known temp file paths
    tempfile_paths = [
        os.path.join(dirpath, fname)
        for dirpath, _, fnames in os.walk(tmpdir)
        for fname in fnames
    ]

    if not tempfile_paths:
        prof.dump_stats(filename)
        return

    # Filter the filenames to remove data from tempfiles, which will
    # have been deleted by the time the results are viewed in a
    # separate process
    stats = prof.get_stats()
    timings = stats.timings
    for key in set(timings):
        fname = key[0]
        try:
            if any(os.path.samefile(fname, tmp) for tmp in tempfile_paths):
                del timings[key]
        except OSError:
            del timings[key]

    with open(filename, 'wb') as f:
        pickle.dump(stats, f, protocol=pickle.HIGHEST_PROTOCOL)


def _main(options, module=False):
    """
    Called by :py:func:`main()` for the actual execution and profiling
    of code;
    not to be invoked on its own.
    """
    def call_with_diagnostics(func, *args, **kwargs):
        if options.debug:
            if isinstance(func, MethodType):
                obj = func.__self__
                func_repr = (
                    '{0.__module__}.{0.__qualname__}(...).{1.__name__}'
                    .format(type(obj), func.__func__))
            else:
                func_repr = '{0.__module__}.{0.__qualname__}'.format(func)
            args_repr = dedent(' ' + pformat(args)[len('['):-len(']')])
            lprefix = len('namespace(')
            kwargs_repr = dedent(
                ' ' * lprefix
                + pformat(SimpleNamespace(**kwargs))[lprefix:-len(')')])
            if args_repr and kwargs_repr:
                all_args_repr = f'{args_repr},\n{kwargs_repr}'
            else:
                all_args_repr = args_repr or kwargs_repr
            if all_args_repr:
                call = '{}(\n{})'.format(
                    func_repr, indent(all_args_repr, '    '))
            else:
                call = func_repr + '()'
            diagnostics.log.debug(f'Calling: {call}')
        if options.dryrun:
            return
        return func(*args, **kwargs)

    if not options.outfile:
        extension = 'lprof' if options.line_by_line else 'prof'
        options.outfile = f'{os.path.basename(options.script)}.{extension}'
        diagnostics.log.debug(
            f'Using default output destination {options.outfile!r}')

    sys.argv = [options.script] + options.args
    if module:
        # Make sure the current directory is on `sys.path` to emulate
        # `python -m`
        # Note: this NEEDS to happen here, before the setup script (or
        # any other code) has a chance to `os.chdir()`
        sys.path.insert(0, os.path.abspath(os.curdir))
    if options.setup is not None:
        # Run some setup code outside of the profiler. This is good for large
        # imports.
        setup_file = find_script(options.setup)
        # Make sure the script's directory is on sys.path instead of just
        # kernprof.py's.
        sys.path.insert(0, os.path.dirname(setup_file))
        ns = {'__file__': setup_file, '__name__': '__main__'}
        diagnostics.log.debug(
            f'Executing file {setup_file!r} as pre-profiling setup')
        if not options.dryrun:
            execfile(setup_file, ns, ns)

    if options.line_by_line:
        import line_profiler
        prof = line_profiler.LineProfiler()
        options.builtin = True
    elif Profile.__module__ == 'profile':
        raise RuntimeError('non-line-by-line profiling depends on cProfile, '
                           'which is not available on this platform')
    else:
        prof = ContextualProfile()

    # If line_profiler is installed, then overwrite the explicit decorator
    try:
        import line_profiler
    except ImportError:  # Shouldn't happen
        install_profiler = global_profiler = None
    else:
        global_profiler = line_profiler.profile
        install_profiler = global_profiler._kernprof_overwrite

    if global_profiler:
        install_profiler(prof)

    if options.builtin:
        builtins.__dict__['profile'] = prof

    if module:
        script_file = find_module_script(options.script)
    else:
        script_file = find_script(options.script)
        # Make sure the script's directory is on sys.path instead of
        # just kernprof.py's.
        sys.path.insert(0, os.path.dirname(script_file))

    # If using eager pre-imports, write a dummy module which contains
    # all those imports and marks them for profiling, then run it
    if options.prof_mod:
        # Note: `prof_mod` entries can be filenames (which can contain
        # commas), so check against existing filenames before splitting
        # them
        options.prof_mod = _normalize_profiling_targets(options.prof_mod)
    if not options.prof_mod:
        options.no_preimports = True
    if options.line_by_line and not options.no_preimports:
        # We assume most items in `.prof_mod` to be import-able without
        # significant side effects, but the same cannot be said if it
        # contains the script file to be run. E.g. the script may not
        # even have a `if __name__ == '__main__': ...` guard. So don't
        # eager-import it.
        exclude = set() if module else {script_file}
        _write_preimports(prof, options, exclude)

    use_timer = options.output_interval and not options.dryrun
    if use_timer:
        rt = RepeatedTimer(max(options.output_interval, 1), prof.dump_stats, options.outfile)
    original_stdout = sys.stdout
    if use_timer:
        rt = RepeatedTimer(max(options.output_interval, 1), prof.dump_stats, options.outfile)
    try:
        try:
            rmod = functools.partial(run_module,
                                     run_name='__main__', alter_sys=True)
            ns = {'__file__': script_file, '__name__': '__main__',
                  'execfile': execfile, 'rmod': rmod,
                  'prof': prof}
            if options.prof_mod and options.line_by_line:
                from line_profiler.autoprofile import autoprofile

                call_with_diagnostics(
                    autoprofile.run, script_file, ns,
                    prof_mod=options.prof_mod,
                    profile_imports=options.prof_imports,
                    as_module=module is not None)
            elif module and options.builtin:
                call_with_diagnostics(rmod, options.script, ns)
            elif options.builtin:
                call_with_diagnostics(execfile, script_file, ns, ns)
            elif module:
                call_with_diagnostics(
                    prof.runctx, f'rmod({options.script!r}, globals())',
                    ns, ns)
            else:
                call_with_diagnostics(
                    prof.runctx, f'execfile({script_file!r}, globals())',
                    ns, ns)
        except (KeyboardInterrupt, SystemExit):
            pass
    finally:
        if use_timer:
            rt.stop()
        if not options.dryrun:
            _dump_filtered_stats(options.tmpdir, prof, options.outfile)
        diagnostics.log.info(
            ('Profile results would have been written to '
             if options.dryrun else
             'Wrote profile results ')
            + f'to {options.outfile!r}')
        if options.verbose > 0 and not options.dryrun:
            if isinstance(prof, ContextualProfile):
                prof.print_stats()
            else:
                prof.print_stats(output_unit=options.unit,
                                 stripzeros=options.skip_zero,
                                 rich=options.rich,
                                 stream=original_stdout)
        else:
            py_exe = _python_command()
            if isinstance(prof, ContextualProfile):
                show_mod = 'pstats'
            else:
                show_mod = 'line_profiler -rmt'
            diagnostics.log.info('Inspect results with:\n'
                                 f'{quote(py_exe)} -m {show_mod} '
                                 f'{quote(options.outfile)}')
        # Fully disable the profiler
        for _ in range(prof.enable_count):
            prof.disable_by_count()
        # Restore the state of the global `@line_profiler.profile`
        if global_profiler:
            install_profiler(None)


if __name__ == '__main__':
    main(sys.argv[1:])
