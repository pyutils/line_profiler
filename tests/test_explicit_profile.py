import contextlib
import os
import re
import sys
import tempfile

import pytest
import ubelt as ub


def test_simple_explicit_nonglobal_usage():
    """
    python -c "from test_explicit_profile import *; test_simple_explicit_nonglobal_usage()"
    """
    from line_profiler import LineProfiler
    profiler = LineProfiler()

    def func(a):
        return a + 1

    profiled_func = profiler(func)

    # Run Once
    profiled_func(1)

    lstats = profiler.get_stats()
    print(f'lstats.timings={lstats.timings}')
    print(f'lstats.unit={lstats.unit}')
    print(f'profiler.code_hash_map={profiler.code_hash_map}')
    profiler.print_stats()


def _demo_explicit_profile_script():
    return ub.codeblock(
        '''
        from line_profiler import profile

        @profile
        def fib(n):
            a, b = 0, 1
            for _ in range(n):
                a, b = b, a + b
            return a
        fib(10)
        ''')


def test_explicit_profile_with_nothing():
    """
    Test that no profiling happens when we dont request it.
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())
    with ub.ChDir(temp_dpath):

        script_fpath = ub.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())

        args = [sys.executable, os.fspath(script_fpath)]
        proc = ub.cmd(args)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    assert not (temp_dpath / 'profile_output.txt').exists()
    assert not (temp_dpath / 'profile_output.lprof').exists()
    temp_dpath.delete()


def test_explicit_profile_with_environ_on():
    """
    Test that explicit profiling is enabled when we specify the LINE_PROFILE
    enviornment variable.
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())
    env = os.environ.copy()
    env['LINE_PROFILE'] = '1'

    with ub.ChDir(temp_dpath):

        script_fpath = ub.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())

        args = [sys.executable, os.fspath(script_fpath)]
        proc = ub.cmd(args, env=env)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    assert (temp_dpath / 'profile_output.txt').exists()
    assert (temp_dpath / 'profile_output.lprof').exists()
    temp_dpath.delete()


def test_explicit_profile_with_environ_off():
    """
    When LINE_PROFILE is falsy, profiling should not run.
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())
    env = os.environ.copy()
    env['LINE_PROFILE'] = '0'

    with ub.ChDir(temp_dpath):

        script_fpath = ub.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())

        args = [sys.executable, os.fspath(script_fpath)]
        proc = ub.cmd(args)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    assert not (temp_dpath / 'profile_output.txt').exists()
    assert not (temp_dpath / 'profile_output.lprof').exists()
    temp_dpath.delete()


def test_explicit_profile_with_cmdline():
    """
    Test that explicit profiling is enabled when we specify the --line-profile
    command line flag.

    xdoctest ~/code/line_profiler/tests/test_explicit_profile.py test_explicit_profile_with_environ
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())

    with ub.ChDir(temp_dpath):

        script_fpath = ub.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())

        args = [sys.executable, os.fspath(script_fpath), '--line-profile']
        print(f'args={args}')
        proc = ub.cmd(args)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    assert (temp_dpath / 'profile_output.txt').exists()
    assert (temp_dpath / 'profile_output.lprof').exists()
    temp_dpath.delete()


@pytest.mark.parametrize('line_profile', [True, False])
def test_explicit_profile_with_kernprof(line_profile: bool):
    """
    Test that explicit profiling works when using kernprof. In this case
    we should get as many output files.
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())
    base_cmd = [sys.executable, '-m', 'kernprof']
    if line_profile:
        base_cmd.append('-l')
        outfile = 'script.py.lprof'
    else:
        outfile = 'script.py.prof'

    with ub.ChDir(temp_dpath):
        script_fpath = ub.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())
        args = base_cmd + [os.fspath(script_fpath)]
        proc = ub.cmd(args)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    assert not (temp_dpath / 'profile_output.txt').exists()
    assert (temp_dpath / outfile).exists()
    temp_dpath.delete()


@pytest.mark.parametrize('package', [True, False])
@pytest.mark.parametrize('builtin', [True, False])
def test_explicit_profile_with_kernprof_m(builtin: bool, package: bool):
    """
    Test that explicit (non-line) profiling works when using
    `kernprof -m` to run packages and/or submodules with relative
    imports.

    Parameters:
        builtin (bool)
            Whether to slip `@profile` into the globals with `--builtin`
            (true) or to require importing it from `line_profiler` in
            the profiled source code (false)

        package (bool)
            Whether to add the code to a package's `__main__.py` and
            `kernprof -m {<package>}` (true), or to add it to a
            submodule and `kernprof -m {<package>}.{<submodule>}`
            (false)
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())

    lib_code = ub.codeblock(
        '''
        @profile
        def func1(a):
            return a + 1

        @profile
        def func2(a):
            return a + 1

        def func3(a):
            return a + 1

        def func4(a):
            return a + 1
        ''').strip()
    if not builtin:
        lib_code = 'from line_profiler import profile\n' + lib_code
    target_code = ub.codeblock(
        '''
        from ._lib import func1, func2, func3, func4

        if __name__ == '__main__':
            func1(1)
            func2(1)
            func3(1)
            func4(1)
        ''').strip()

    if package:
        target_module = 'package'
        target_fname = '__main__.py'
    else:
        target_module = 'package.api'
        target_fname = 'api.py'

    args = ['kernprof', '-v', '-m', target_module]
    if builtin:
        args.insert(2, '--builtin')  # Insert before the `-m` flag

    if 'PYTHONPATH' in os.environ:
        python_path = '{}:{}'.format(os.environ['PYTHONPATH'], os.curdir)
    else:
        python_path = os.curdir
    env = {**os.environ, 'PYTHONPATH': python_path}

    with ub.ChDir(temp_dpath):
        package_dir = ub.Path('package').mkdir()

        lib_fpath = package_dir / '_lib.py'
        lib_fpath.write_text(lib_code)

        target_fpath = package_dir / target_fname
        target_fpath.write_text(target_code)

        (package_dir / '__init__.py').touch()

        proc = ub.cmd(args, env=env)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    # Note: in non-builtin mode, the entire script is profiled
    for func, profiled in [('func1', True), ('func2', True),
                           ('func3', not builtin), ('func4', not builtin)]:
        result = re.search(r'lib\.py:[0-9]+\({}\)'.format(func), proc.stdout)
        assert bool(result) == profiled

    assert not (temp_dpath / 'profile_output.txt').exists()
    assert (temp_dpath / (target_module + '.prof')).exists()
    temp_dpath.delete()


def test_explicit_profile_with_in_code_enable():
    """
    Test that the user can enable the profiler explicitly from within their
    code.

    CommandLine:
        pytest tests/test_explicit_profile.py -s -k test_explicit_profile_with_in_code_enable
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())

    code = ub.codeblock(
        '''
        from line_profiler import profile
        import ubelt as ub
        print('')
        print('')
        print('start test')

        print('profile = {}'.format(ub.urepr(profile, nl=1)))
        print(f'profile._profile={profile._profile}')
        print(f'profile.enabled={profile.enabled}')

        @profile
        def func1(a):
            return a + 1

        profile.enable(output_prefix='custom_output')

        print('profile = {}'.format(ub.urepr(profile, nl=1)))
        print(f'profile._profile={profile._profile}')
        print(f'profile.enabled={profile.enabled}')

        @profile
        def func2(a):
            return a + 1

        print('func2 = {}'.format(ub.urepr(func2, nl=1)))

        profile.disable()

        @profile
        def func3(a):
            return a + 1

        profile.enable()

        @profile
        def func4(a):
            return a + 1

        func1(1)
        func2(1)
        func3(1)
        func4(1)

        profile._profile
        ''')
    with ub.ChDir(temp_dpath):

        script_fpath = ub.Path('script.py')
        script_fpath.write_text(code)

        args = [sys.executable, os.fspath(script_fpath)]
        proc = ub.cmd(args)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    print('Finished running script')

    output_fpath = (temp_dpath / 'custom_output.txt')
    raw_output = output_fpath.read_text()
    print(f'Contents of {output_fpath}')
    print(raw_output)

    assert 'Function: func1' not in raw_output
    assert 'Function: func2' in raw_output
    assert 'Function: func3' not in raw_output
    assert 'Function: func4' in raw_output

    assert output_fpath.exists()
    assert (temp_dpath / 'custom_output.lprof').exists()
    temp_dpath.delete()


def test_explicit_profile_with_duplicate_functions():
    """
    Test profiling duplicate functions with the explicit profiler

    CommandLine:
        pytest -sv tests/test_explicit_profile.py -k test_explicit_profile_with_duplicate_functions
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())

    code = ub.codeblock(
        '''
        from line_profiler import profile

        @profile
        def func1(a):
            return a + 1

        @profile
        def func2(a):
            return a + 1

        @profile
        def func3(a):
            return a + 1

        @profile
        def func4(a):
            return a + 1

        func1(1)
        func2(1)
        func3(1)
        func4(1)
        ''').strip()
    with ub.ChDir(temp_dpath):

        script_fpath = ub.Path('script.py')
        script_fpath.write_text(code)

        args = [sys.executable, os.fspath(script_fpath), '--line-profile']
        proc = ub.cmd(args)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    output_fpath = (temp_dpath / 'profile_output.txt')
    raw_output = output_fpath.read_text()
    print(raw_output)

    assert 'Function: func1' in raw_output
    assert 'Function: func2' in raw_output
    assert 'Function: func3' in raw_output
    assert 'Function: func4' in raw_output

    assert output_fpath.exists()
    assert (temp_dpath / 'profile_output.lprof').exists()
    temp_dpath.delete()


@pytest.mark.parametrize('reset_enable_count', [True, False])
@pytest.mark.parametrize('wrap_class, wrap_module',
                         [(None, None), (False, True),
                          (True, False), (True, True)])
def test_profiler_add_methods(wrap_class, wrap_module, reset_enable_count):
    """
    Test the `wrap` argument for the
    `line_profiler.autoprofile.autoprofile.
    _extend_line_profiler_for_profiling_imports()`,
    `LineProfiler.add_class()` and `.add_module()` methods.
    """
    def write(path, code):
        path.write_text(ub.codeblock(code))

    script = ub.codeblock('''
        from line_profiler import LineProfiler
        from line_profiler.autoprofile.autoprofile import (
            _extend_line_profiler_for_profiling_imports as upgrade_profiler)

        import my_module_1
        from my_module_2 import Class
        from my_module_3 import func3


        profiler = LineProfiler()
        upgrade_profiler(profiler)
        profiler.add_imported_function_or_module(my_module_1{})
        profiler.add_imported_function_or_module(Class{})
        profiler.add_imported_function_or_module(func3)

        if {}:
            for _ in range(profiler.enable_count):
                profiler.disable_by_count()

        # `func1()` should only have timing info if `wrap_module`
        my_module_1.func1()
        # `method2()` should only have timing info if `wrap_class`
        Class.method2()
        # `func3()` is profiled but don't see any timing info because it
        # isn't wrapped and doesn't auto-`.enable()` before being called
        func3()
        profiler.print_stats(details=True, summarize=True)
                          '''.format(
        '' if wrap_module is None else f', wrap={wrap_module}',
        '' if wrap_class is None else f', wrap={wrap_class}',
        reset_enable_count))

    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        enter(ub.ChDir(enter(tempfile.TemporaryDirectory())))
        curdir = ub.Path.cwd()
        write(curdir / 'script.py', script)
        write(curdir / 'my_module_1.py',
              '''
        def func1():
            pass  # Marker: func1
              ''')
        write(curdir / 'my_module_2.py',
              '''
        class Class:
            @classmethod
            def method2(cls):
                pass  # Marker: method2
              ''')
        write(curdir / 'my_module_3.py',
              '''
        def func3():
            pass  # Marker: func3
              ''')
        proc = ub.cmd([sys.executable, str(curdir / 'script.py')])

    # Check that the profiler has seen each of the methods
    raw_output = proc.stdout
    print(script)
    print(raw_output)
    print(proc.stderr)
    proc.check_returncode()
    assert '# Marker: func1' in raw_output
    assert '# Marker: method2' in raw_output
    assert '# Marker: func3' in raw_output

    # Check that the timing info (of the lack thereof) are correct
    for func, has_timing in [('func1', wrap_module), ('method2', wrap_class),
                             ('func3', False)]:
        line, = (line for line in raw_output.splitlines()
                 if line.endswith('Marker: ' + func))
        has_timing = has_timing or not reset_enable_count
        assert line.split()[1] == ('1' if has_timing else 'pass')


if __name__ == '__main__':
    ...
    test_simple_explicit_nonglobal_usage()
