#!/usr/bin/env python
"""
Script to conveniently run profilers on code in a variety of circumstances.

To profile a script, decorate the functions of interest with ``@profile``

.. code:: bash

    echo "if 1:
        @profile
        def main():
            1 + 1
        main()
    " > script_to_profile.py

NOTE:

    New in 4.1.0: Instead of relying on injecting ``profile`` into the builtins
    you can now ``import line_profiler`` and use ``line_profiler.profile`` to
    decorate your functions. This allows the script to remain functional even
    if it is not actively profiled. See :py:mod:`line_profiler` for details.


Then run the script using kernprof:

.. code:: bash

    kernprof -b script_to_profile.py

By default this runs with the default :py:mod:`cProfile` profiler and does not
require compiled modules. Instructions to view the results will be given in the
output. Alternatively, adding ``-v`` to the command line will write results to
stdout.

To enable line-by-line profiling, then :py:mod:`line_profiler` must be
available and compiled. Add the ``-l`` argument to the kernprof invocation.

.. code:: bash

    kernprof -lb script_to_profile.py

NOTE:

    New in 4.3.0: more code execution options are added:

    * ``kernprof <options> -m some.module <args to module>`` parallels
      ``python -m`` and runs the provided module as ``__main__``.
    * ``kernprof <options> -c "some code" <args to code>`` parallels
      ``python -c`` and executes the provided literal code.
    * ``kernprof <options> - <args to code>`` parallels ``python -`` and
      executes literal code passed via the ``stdin``.

    See also :doc:`kernprof invocations </manual/examples/example_kernprof>`.

For more details and options, refer to the CLI help.
To view kernprof help run:

.. code:: bash

    kernprof --help

which displays:

.. code::

    usage: kernprof [-h] [-V] [-l] [-b] [-o OUTFILE] [-s SETUP] [-v] [-r] [-u UNIT] [-z] [-i [OUTPUT_INTERVAL]] [-p PROF_MOD] [--prof-imports]
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
      -v, --view            View the results of the profile in addition to saving it
      -r, --rich            Use rich formatting if viewing output
      -u, --unit UNIT       Output unit (in seconds) in which the timing info is displayed (default: 1e-6)
      -z, --skip-zero       Hide functions which have not been called
      -i, --output-interval [OUTPUT_INTERVAL]
                            Enables outputting of cumulative profiling results to file every n seconds. Uses the threading module. Minimum value is 1 (second). Defaults to
                            disabled.
      -p, --prof-mod PROF_MOD
                            List of modules, functions and/or classes to profile specified by their name or path. List is comma separated, adding the current script path profiles
                            the full script. Multiple copies of this flag can be supplied and the.list is extended. Only works with line_profiler -l, --line-by-line
      --prof-imports        If specified, modules specified to `--prof-mod` will also autoprofile modules that they import. Only works with line_profiler -l, --line-by-line
"""
import builtins
import functools
import os
import sys
import threading
import asyncio  # NOQA
import concurrent.futures  # NOQA
import tempfile
import time
from argparse import ArgumentError, ArgumentParser
from runpy import run_module

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


def execfile(filename, globals=None, locals=None):
    """ Python 3.x doesn't have 'execfile' builtin """
    with open(filename, 'rb') as f:
        exec(compile(f.read(), filename, 'exec'), globals, locals)
# =====================================


class ContextualProfile(ByCountProfilerMixin, Profile):
    """ A subclass of Profile that adds a context manager for Python
    2.5 with: statements and a decorator.
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
    Background timer for outputting file every n seconds.

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


def find_script(script_name):
    """ Find the script.

    If the input is not a file, then $PATH will be searched.
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

    sys.stderr.write('Could not find script %s\n' % script_name)
    raise SystemExit(1)


def _python_command():
    """
    Return a command that corresponds to :py:obj:`sys.executable`.
    """
    import shutil
    for abbr in 'python', 'python3':
        if os.path.samefile(shutil.which(abbr), sys.executable):
            return abbr
    return sys.executable


class _restore_list:
    """
    Restore a list like `sys.path` after running code which potentially
    modifies it.

    Example
    -------
    >>> l = [1, 2, 3]
    >>>
    >>>
    >>> with _restore_list(l):
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
    def __init__(self, lst):
        self.lst = lst
        self.old = None

    def __enter__(self):
        assert self.old is None
        self.old = self.lst.copy()

    def __exit__(self, *_, **__):
        self.old, self.lst[:] = None, self.old

    def __call__(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)

        return wrapper


def pre_parse_single_arg_directive(args, flag, sep='--'):
    """
    Pre-parse high-priority single-argument directives like `-m module`
    to emulate the behavior of `python [...]`.

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


@_restore_list(sys.argv)
@_restore_list(sys.path)
def main(args=None):
    """
    Runs the command line interface
    """
    def positive_float(value):
        val = float(value)
        if val <= 0:
            raise ArgumentError
        return val

    create_parser = functools.partial(
        ArgumentParser,
        description='Run and profile a python script.')

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
        real_parser, = parsers = [create_parser()]
        help_parser = None
    else:
        # We've already consumed the `-m <module>`, so we need a dummy
        # parser for generating the help text;
        # but the real parser should not consume the `options.script`
        # positional arg, and it it got the `--help` option, it should
        # hand off the the dummy parser
        real_parser = create_parser(add_help=False)
        real_parser.add_argument('-h', '--help', action='store_true')
        help_parser = create_parser()
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
        parser.add_argument('-v', '--view', action='store_true',
                            help='View the results of the profile in addition to saving it')
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
        parser.add_argument('-p', '--prof-mod', action='append', type=str,
                            help="List of modules, functions and/or classes to profile specified by their name or path. "
                            "List is comma separated, adding the current script path profiles the full script. "
                            "Multiple copies of this flag can be supplied and the.list is extended. "
                            "Only works with line_profiler -l, --line-by-line")
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
    options = real_parser.parse_args(args)
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

    if tempfile_source_and_content:
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tempfile(*tempfile_source_and_content, options, tmpdir)
            return _main(options, module)
    else:
        return _main(options, module)


def _write_tempfile(source, content, options, tmpdir):
    """
    Called by ``main()`` to handle ``kernprof -c`` and ``kernprof -``;
    not to be invoked on its own.
    """
    import textwrap

    # Set up the script to be run
    file_prefix = f'kernprof-{source}'
    # Do what 3.14 does (#103998)... and also just to be user-friendly
    content = textwrap.dedent(content)
    fname = os.path.join(tmpdir, file_prefix + '.py')
    with open(fname, mode='w') as fobj:
        print(content, file=fobj)
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
        _, options.outfile = tempfile.mkstemp(dir=os.curdir,
                                              prefix=file_prefix + '-',
                                              suffix='.' + extension)


def _main(options, module=False):
    """
    Called by ``main()`` for the actual execution and profiling of code;
    not to be invoked on its own.
    """
    if not options.outfile:
        extension = 'lprof' if options.line_by_line else 'prof'
        options.outfile = '%s.%s' % (os.path.basename(options.script), extension)

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
        __file__ = setup_file
        __name__ = '__main__'
        # Make sure the script's directory is on sys.path instead of just
        # kernprof.py's.
        sys.path.insert(0, os.path.dirname(setup_file))
        ns = locals()
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
    __file__ = script_file
    __name__ = '__main__'

    if options.output_interval:
        rt = RepeatedTimer(max(options.output_interval, 1), prof.dump_stats, options.outfile)
    original_stdout = sys.stdout
    if options.output_interval:
        rt = RepeatedTimer(max(options.output_interval, 1), prof.dump_stats, options.outfile)
    try:
        try:
            execfile_ = execfile
            rmod_ = functools.partial(run_module,
                                      run_name='__main__', alter_sys=True)
            ns = locals()
            if options.prof_mod and options.line_by_line:
                from line_profiler.autoprofile import autoprofile
                # Note: `prof_mod` entries can be filenames (which can
                # contain commas), so check against existing filenames
                # before splitting them
                prof_mod = sum(
                    ([spec] if os.path.exists(spec) else spec.split(',')
                     for spec in options.prof_mod),
                    [])
                autoprofile.run(script_file,
                                ns,
                                prof_mod=prof_mod,
                                profile_imports=options.prof_imports,
                                as_module=module is not None)
            elif module and options.builtin:
                rmod_(options.script, ns)
            elif options.builtin:
                execfile(script_file, ns, ns)
            elif module:
                prof.runctx(f'rmod_({options.script!r}, globals())', ns, ns)
            else:
                prof.runctx('execfile_(%r, globals())' % (script_file,), ns, ns)
        except (KeyboardInterrupt, SystemExit):
            pass
    finally:
        if options.output_interval:
            rt.stop()
        prof.dump_stats(options.outfile)
        print('Wrote profile results to %s' % options.outfile)
        if options.view:
            if isinstance(prof, ContextualProfile):
                prof.print_stats()
            else:
                prof.print_stats(output_unit=options.unit,
                                 stripzeros=options.skip_zero,
                                 rich=options.rich,
                                 stream=original_stdout)
        else:
            print('Inspect results with:')
            py_exe = _python_command()
            if isinstance(prof, ContextualProfile):
                print(f'{py_exe} -m pstats "{options.outfile}"')
            else:
                print(f'{py_exe} -m line_profiler -rmt "{options.outfile}"')
        # Restore the state of the global `@line_profiler.profile`
        if global_profiler:
            install_profiler(None)


if __name__ == '__main__':
    main(sys.argv[1:])
