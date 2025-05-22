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

    usage: kernprof [-h] [-V] [--config CONFIG] [--no-config]
                    [--line-by-line [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]]
                    [--builtin [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]]
                    [-s SETUP] [-p PROF_MOD]
                    [--prof-imports [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]]
                    [-o OUTFILE]
                    [--view [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]]
                    [--rich [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]]
                    [-u UNIT]
                    [--skip-zero [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]]
                    [-i [OUTPUT_INTERVAL]]
                    {script | -m module} ...

    Run and profile a python script or module.

    positional arguments:
      {path/to/script | -m path.to.module | -c "literal code"}
                            The python script file, module, or literal code to run
      args                  Optional script arguments

    options:
      -h, --help            show this help message and exit
      -V, --version         show program's version number and exit
      --config CONFIG       Path to the TOML file, from the `tool.line_profiler.kernprof`
                            table of which to load defaults for the options. (Default:
                            'pyproject.toml')
      --no-config           Disable the loading of configuration files other than the
                            default one

    profiling options:
      --line-by-line [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]
                            Use the line-by-line profiler instead of cProfile. Implies
                            `--builtin`. (Default: False; short form: -l)
      --builtin [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]
                            Put `profile` in the builtins. Use
                            `profile.enable()`/`.disable()` to toggle profiling,
                            `@profile` to decorate functions, or `with profile:` to
                            profile a section of code. (Default: False; short form: -b)
      -s, --setup SETUP     Path to the Python source file containing setup code to
                            execute before the code to profile. (Default: N/A)
      -p, --prof-mod PROF_MOD
                            List of modules, functions and/or classes to profile specified
                            by their name or path. List is comma separated, adding the
                            current script path profiles the full script. Multiple copies
                            of this flag can be supplied and the list is extended (e.g.
                            `-p this.module,another.module -p some.func`). Only works with
                            line profiling (`-l`/`--line-by-line`). (Default: N/A)
      --prof-imports [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]
                            If the script/module profiled is in `--prof-mod`, autoprofile
                            all its imports. Only works with line profiling (`-l`/`--line-
                            by-line`). (Default: False)

    output options:
      -o, --outfile OUTFILE
                            Save stats to OUTFILE. (Default:
                            '<script_or_module_name>.lprof' in line-profiling mode
                            (`-l`/`--line-by-line`); '<script_or_module_name>.prof'
                            otherwise)
      --view [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]
                            View the results of the profile in addition to saving it.
                            (Default: False; short form: -v)
      --rich [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]
                            Use rich formatting if viewing output. (Default: False; short
                            form: -r)
      -u, --unit UNIT       Output unit (in seconds) in which the timing info is
                            displayed. (Default: 1e-06 s)
      --skip-zero [Y[es] | N[o] | T[rue] | F[alse] | on | off | 1 | 0]
                            Hide functions which have not been called. (Default: False;
                            short form: -z)
      -i, --output-interval [OUTPUT_INTERVAL]
                            Enables outputting of cumulative profiling results to OUTFILE
                            every OUTPUT_INTERVAL seconds. Uses the threading module.
                            Minimum value (and the value implied if the bare option is
                            given) is 1 s. (Default: 0 s (disabled))
"""  # noqa: E501
import argparse
import builtins
import functools
import os
import sys
import threading
import asyncio  # NOQA
import concurrent.futures  # NOQA
import tempfile
import time
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

import line_profiler
from line_profiler.cli_utils import (
    add_argument, get_cli_config,
    get_python_executable as _python_command,  # Compatibility
    positive_float, short_string_path)
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
    """  # noqa: E501
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
            self._timer = threading.Timer(self.next_call - time.time(),
                                          self._run)
            self._timer.start()
            self.is_running = True

    def stop(self):
        self._timer.cancel()
        self.is_running = False


def find_module_script(module_name, *, exit_on_error=True):
    """Find the path to the executable script for a module or package."""
    from line_profiler.autoprofile.util_static import modname_to_modpath

    for suffix in '.__main__', '':
        fname = modname_to_modpath(module_name + suffix)
        if fname:
            return fname

    msg = f'Could not find module `{module_name}`'
    if exit_on_error:
        print(msg, file=sys.stderr)
        raise SystemExit(1)
    else:
        raise ModuleNotFoundError(msg)


def find_script(script_name, *, exit_on_error=True):
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

    msg = f'Could not find script {script_name!r}'
    if exit_on_error:
        print(msg, file=sys.stderr)
        raise SystemExit(1)
    else:
        raise FileNotFoundError(msg)


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
def main(args=None, exit_on_error=True):
    """
    Runs the command line interface
    """
    create_parser = functools.partial(
        argparse.ArgumentParser,
        description='Run and profile a python script or module.')
    get_kernprof_config = functools.partial(get_cli_config, 'kernprof')
    defaults, default_source = get_kernprof_config()

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
        add_argument(parser, '-V', '--version',
                     action='version', version=__version__)
        add_argument(parser, '--config',
                     help='Path to the TOML file, from the '
                     '`tool.line_profiler.kernprof` table of which to load '
                     'defaults for the options. '
                     f'(Default: {short_string_path(default_source)!r})')
        add_argument(parser, '--no-config',
                     action='store_const', dest='config', const=False,
                     help='Disable the loading of configuration files other '
                     'than the default one')
        prof_opts = parser.add_argument_group('profiling options')
        add_argument(prof_opts, '-l', '--line-by-line', action='store_true',
                     help='Use the line-by-line profiler instead of cProfile. '
                     'Implies `--builtin`. '
                     f'(Default: {defaults["line_by_line"]})')
        add_argument(prof_opts, '-b', '--builtin', action='store_true',
                     help="Put `profile` in the builtins. "
                     "Use `profile.enable()`/`.disable()` to "
                     "toggle profiling, "
                     "`@profile` to decorate functions, "
                     "or `with profile:` to profile a section of code. "
                     f"(Default: {defaults['builtin']})")
        if defaults['setup']:
            def_setupfile = repr(defaults['setup'])
        else:
            def_setupfile = 'N/A'
        add_argument(prof_opts, '-s', '--setup',
                     help='Path to the Python source file containing setup '
                     'code to execute before the code to profile. '
                     f'(Default: {def_setupfile})')
        if defaults['prof_mod']:
            def_prof_mod = repr(defaults['prof_mod'])
        else:
            def_prof_mod = 'N/A'
        add_argument(prof_opts, '-p', '--prof-mod', action='append',
                     help="List of modules, functions and/or classes "
                     "to profile specified by their name or path. "
                     "List is comma separated, adding the current script path "
                     "profiles the full script. "
                     "Multiple copies of this flag can be supplied and "
                     "the list is extended "
                     "(e.g. `-p this.module,another.module -p some.func`). "
                     "Only works with line profiling (`-l`/`--line-by-line`). "
                     f"(Default: {def_prof_mod})")
        add_argument(prof_opts, '--prof-imports', action='store_true',
                     help="If the script/module profiled is in `--prof-mod`, "
                     "autoprofile all its imports. "
                     "Only works with line profiling (`-l`/`--line-by-line`). "
                     f"(Default: {defaults['prof_imports']})")
        out_opts = parser.add_argument_group('output options')
        if defaults['outfile']:
            def_outfile = repr(defaults['outfile'])
        else:
            def_outfile = (
                "'<script_or_module_name>.lprof' in line-profiling mode "
                "(`-l`/`--line-by-line`); "
                "'<script_or_module_name>.prof' otherwise")
        add_argument(out_opts, '-o', '--outfile',
                     help=f'Save stats to OUTFILE. (Default: {def_outfile})')
        add_argument(out_opts, '-v', '--view', action='store_true',
                     help='View the results of the profile '
                     'in addition to saving it. '
                     f'(Default: {defaults["view"]})')
        add_argument(out_opts, '-r', '--rich', action='store_true',
                     help='Use rich formatting if viewing output. '
                     f'(Default: {defaults["rich"]})')
        add_argument(out_opts, '-u', '--unit', type=positive_float,
                     help='Output unit (in seconds) in which '
                     'the timing info is displayed. '
                     f'(Default: {defaults["unit"]} s)')
        add_argument(out_opts, '-z', '--skip-zero', action='store_true',
                     help="Hide functions which have not been called. "
                     f"(Default: {defaults['skip_zero']})")
        if defaults['output_interval']:
            def_out_int = f'{defaults["output_interval"]} s'
        else:
            def_out_int = '0 s (disabled)'
        add_argument(out_opts, '-i', '--output-interval',
                     type=int, const=1, nargs='?',
                     help="Enables outputting of cumulative profiling results "
                     "to OUTFILE every OUTPUT_INTERVAL seconds. "
                     "Uses the threading module. "
                     "Minimum value (and the value implied if the bare option "
                     f"is given) is 1 s. (Default: {def_out_int})")

        if parser is help_parser or module is literal_code is None:
            add_argument(parser, 'script',
                         metavar='{path/to/script'
                         ' | -m path.to.module | -c "literal code"}',
                         help='The python script file, module, or '
                         'literal code to run')
        add_argument(parser, 'args',
                     nargs='...', help='Optional script arguments')

    # Hand off to the dummy parser if necessary to generate the help
    # text
    try:
        options = real_parser.parse_args(args)
    except SystemExit as e:
        # If `exit_on_error` is true, let `SystemExit` bubble up and
        # kill the interpretor;
        # else, catch and handle it more gracefully
        # (Note: can't use `ArgumentParser(exit_on_error=False)` in
        # Python 3.8)
        if exit_on_error:
            raise
        elif e.code:
            raise RuntimeError from None
        else:
            return
    if help_parser and getattr(options, 'help', False):
        help_parser.print_help()
        if exit_on_error:
            raise SystemExit(0)
        else:
            return

    # Parse the provided config file (if any), and resolve the values
    # of the un-specified options
    try:
        del options.help
    except AttributeError:
        pass
    if options.config:
        defaults, options.config = get_kernprof_config(options.config)
    for key, default in defaults.items():
        if getattr(options, key, None) is None:
            setattr(options, key, default)

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
            return _main(options, module, exit_on_error)
    else:
        return _main(options, module, exit_on_error)


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


def _main(options, module=False, exit_on_error=True):
    """
    Called by ``main()`` for the actual execution and profiling of code;
    not to be invoked on its own.
    """
    if not options.outfile:
        extension = 'lprof' if options.line_by_line else 'prof'
        options.outfile = f'{os.path.basename(options.script)}.{extension}'

    sys.argv = [options.script] + options.args
    if module:
        # Make sure the current directory is on `sys.path` to emulate
        # `python -m`
        # Note: this NEEDS to happen here, before the setup script (or
        # any other code) has a chance to `os.chdir()`
        sys.path.insert(0, os.path.abspath(os.curdir))
    if options.setup:
        # Run some setup code outside of the profiler. This is good for
        # large imports.
        setup_file = find_script(options.setup, exit_on_error=exit_on_error)
        __file__ = setup_file
        __name__ = '__main__'
        # Make sure the script's directory is on sys.path instead of just
        # kernprof.py's.
        sys.path.insert(0, os.path.dirname(setup_file))
        ns = locals()
        execfile(setup_file, ns, ns)

    if options.line_by_line:
        prof = line_profiler.LineProfiler()
        options.builtin = True
    elif Profile.__module__ == 'profile':
        raise RuntimeError('non-line-by-line profiling depends on cProfile, '
                           'which is not available on this platform')
    else:
        prof = ContextualProfile()

    # Overwrite the explicit decorator
    global_profiler = line_profiler.profile
    install_profiler = global_profiler._kernprof_overwrite
    install_profiler(prof)

    if options.builtin:
        builtins.__dict__['profile'] = prof

    if module:
        script_file = find_module_script(options.script,
                                         exit_on_error=exit_on_error)
    else:
        script_file = find_script(options.script, exit_on_error=exit_on_error)
        # Make sure the script's directory is on sys.path instead of
        # just kernprof.py's.
        sys.path.insert(0, os.path.dirname(script_file))
    __file__ = script_file
    __name__ = '__main__'

    original_stdout = sys.stdout
    if options.output_interval:
        rt = RepeatedTimer(max(options.output_interval, 1), prof.dump_stats,
                           options.outfile)
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
            else:
                if module:
                    runner, target = 'rmod_', options.script
                else:
                    runner, target = 'execfile_', script_file
                assert runner in ns
                if options.builtin:
                    ns[runner](target, ns)
                else:
                    prof.runctx(f'{runner}({target!r}, globals())', ns, ns)
        except (KeyboardInterrupt, SystemExit):
            pass
    finally:
        if options.output_interval:
            rt.stop()
        prof.dump_stats(options.outfile)
        short_outfile = short_string_path(options.outfile)
        print(f'Wrote profile results to {short_outfile!r}')
        if options.view:
            if isinstance(prof, ContextualProfile):
                prof.print_stats()
            else:
                prof.print_stats(output_unit=options.unit,
                                 stripzeros=options.skip_zero,
                                 rich=options.rich,
                                 stream=original_stdout,
                                 config=options.config)
        else:
            print('Inspect results with:')
            py_exe = _python_command()
            if isinstance(prof, ContextualProfile):
                print(f'{py_exe} -m pstats {short_outfile!r}')
            else:
                print(f'{py_exe} -m line_profiler -rmt {short_outfile!r}')
        # Restore the state of the global `@line_profiler.profile`
        install_profiler(None)


if __name__ == '__main__':
    main()
