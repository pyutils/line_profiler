import os
import re
import sys
import tempfile
from contextlib import ExitStack

import pytest
import ubelt as ub


class enter_tmpdir:
    """
    Set up a temporary directory and :cmd:`chdir` into it.
    """
    def __init__(self):
        self.stack = ExitStack()

    def __enter__(self):
        """
        Returns:
            curdir (ubelt.Path)
                Temporary directory :cmd:`chdir`-ed into.

        Side effects:
            ``curdir`` created and :cmd:`chdir`-ed into.
        """
        enter = self.stack.enter_context
        tmpdir = os.path.abspath(enter(tempfile.TemporaryDirectory()))
        enter(ub.ChDir(tmpdir))
        return ub.Path(tmpdir)

    def __exit__(self, *_, **__):
        """
        Side effects:
            * Original working directory restored.
            * Temporary directory created deleted.
        """
        self.stack.close()


class restore_sys_modules:
    """
    Restore :py:attr:`sys.modules` after exiting the context.
    """
    def __enter__(self):
        self.old = sys.modules.copy()

    def __exit__(self, *_, **__):
        sys.modules.clear()
        sys.modules.update(self.old)


def write(path, code=None):
    path.parent.mkdir(exist_ok=True, parents=True)
    if code is None:
        path.touch()
    else:
        path.write_text(ub.codeblock(code))


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
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
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


def test_explicit_profile_with_environ_on():
    """
    Test that explicit profiling is enabled when we specify the LINE_PROFILE
    enviornment variable.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
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


def test_explicit_profile_ignores_inherited_owner_marker():
    """
    Standalone runs should not be blocked by an inherited owner marker.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
        env = os.environ.copy()
        env['LINE_PROFILE'] = '1'
        env['LINE_PROFILER_OWNER_PID'] = str(os.getpid() + 100000)
        env['PYTHONPATH'] = os.getcwd()

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


def test_explicit_profile_process_pool_forkserver():
    """
    Ensure explicit profiler works with forkserver ProcessPoolExecutor.
    """
    import multiprocessing as mp
    if 'forkserver' not in mp.get_all_start_methods():
        pytest.skip('forkserver start method not available')
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
        env = os.environ.copy()
        env['LINE_PROFILE'] = '1'
        env['LINE_PROFILER_DEBUG'] = '1'
        env['PYTHONPATH'] = os.getcwd()

        with ub.ChDir(temp_dpath):

            script_fpath = ub.Path('script.py')
            script_fpath.write_text(ub.codeblock(
                '''
                import multiprocessing as mp
                from concurrent.futures import ProcessPoolExecutor
                from line_profiler import profile

                def worker(x):
                    return x * x

                @profile
                def run():
                    total = 0
                    for i in range(1000):
                        total += i % 7
                    with ProcessPoolExecutor(max_workers=2) as ex:
                        list(ex.map(worker, range(4)))
                    return total

                def main():
                    if 'forkserver' in mp.get_all_start_methods():
                        mp.set_start_method('forkserver', force=True)
                    run()

                if __name__ == '__main__':
                    main()
                ''').strip())

            args = [sys.executable, os.fspath(script_fpath)]
            proc = ub.cmd(args, env=env)
            print(proc.stdout)
            print(proc.stderr)
            proc.check_returncode()

        output_path = temp_dpath / 'profile_output.txt'
        assert output_path.exists()
        assert output_path.stat().st_size > 100
        assert proc.stdout.count('Wrote profile results to profile_output.txt') == 1


def test_explicit_profile_with_environ_off():
    """
    When LINE_PROFILE is falsy, profiling should not run.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
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


def test_explicit_profile_with_cmdline():
    """
    Test that explicit profiling is enabled when we specify the --line-profile
    command line flag.

    xdoctest ~/code/line_profiler/tests/test_explicit_profile.py test_explicit_profile_with_environ
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
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


@pytest.mark.parametrize('line_profile', [True, False])
def test_explicit_profile_with_kernprof(line_profile: bool):
    """
    Test that explicit profiling works when using kernprof. In this case
    we should get as many output files.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
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
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

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
            result = re.search(r'lib\.py:[0-9]+\({}\)'.format(func),
                               proc.stdout)
            assert bool(result) == profiled

        assert not (temp_dpath / 'profile_output.txt').exists()
        assert (temp_dpath / (target_module + '.prof')).exists()


def test_explicit_profile_with_in_code_enable():
    """
    Test that the user can enable the profiler explicitly from within their
    code.

    CommandLine:
        pytest tests/test_explicit_profile.py -s -k test_explicit_profile_with_in_code_enable
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

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


def test_explicit_profile_with_duplicate_functions():
    """
    Test profiling duplicate functions with the explicit profiler

    CommandLine:
        pytest -sv tests/test_explicit_profile.py -k test_explicit_profile_with_duplicate_functions
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

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


def test_explicit_profile_with_customized_config():
    """
    Test that explicit profiling can be configured with the appropriate
    TOML file.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

        env = os.environ.copy()
        env['PROFILE'] = '1'

        with ub.ChDir(temp_dpath):
            script_fpath = ub.Path('script.py')
            script_fpath.write_text(_demo_explicit_profile_script())
            toml = ub.Path('my_config.toml')
            toml.write_text(ub.codeblock('''
        [tool.line_profiler.setup]
        environ_flags = ['PROFILE']

        [tool.line_profiler.write]
        output_prefix = 'my_profiling_results'
        timestamped_text = false

        [tool.line_profiler.show]
        details = true
        summarize = false
            '''))

            env['LINE_PROFILER_RC'] = str(toml)
            args = [sys.executable, os.fspath(script_fpath)]
            proc = ub.cmd(args, env=env)
            print(proc.stdout)
            print(proc.stderr)
            proc.check_returncode()

        # Check the `write` config
        assert set(os.listdir(temp_dpath)) == {'script.py',
                                               'my_config.toml',
                                               'my_profiling_results.lprof',
                                               'my_profiling_results.txt'}
        # Check the `show` config
        assert '- fib' not in proc.stdout  # No summary
        assert 'Function: fib' in proc.stdout  # With details


@pytest.mark.parametrize('reset_enable_count', [True, False])
@pytest.mark.parametrize('wrap_class, wrap_module',
                         [(None, None), (False, True),
                          (True, False), (True, True)])
def test_profiler_add_methods(wrap_class, wrap_module, reset_enable_count):
    """
    Test the `wrap` argument for the
    `LineProfiler.add_class()`, `.add_module()`, and
    `.add_imported_function_or_module()` (added via
    `line_profiler.autoprofile.autoprofile.
    _extend_line_profiler_for_profiling_imports()`) methods.
    """
    script = ub.codeblock('''
        from line_profiler import LineProfiler
        from line_profiler.autoprofile.autoprofile import (
            _extend_line_profiler_for_profiling_imports as upgrade_profiler)

        import my_module_1
        from my_module_2 import Class
        from my_module_3 import func3


        profiler = LineProfiler()
        upgrade_profiler(profiler)
        # This dispatches to `.add_module()`
        profiler.add_imported_function_or_module(my_module_1{})
        # This dispatches to `.add_class()`
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

    with enter_tmpdir() as curdir:
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


def test_profiler_add_class_recursion_guard():
    """
    Test that if we were to add a pair of classes which each of them
    has a reference to the other in its namespace, we don't end up in
    infinite recursion.
    """
    from line_profiler import LineProfiler

    class Class1:
        def method1(self):
            pass

        class ChildClass1:
            def child_method_1(self):
                pass

    class Class2:
        def method2(self):
            pass

        class ChildClass2:
            def child_method_2(self):
                pass

        OtherClass = Class1
        # A duplicate reference shouldn't affect profiling either
        YetAnotherClass = Class1

    # Add self/mutual references
    Class1.ThisClass = Class1
    Class1.OtherClass = Class2

    profile = LineProfiler()
    profile.add_class(Class1)
    assert len(profile.functions) == 4
    assert Class1.method1 in profile.functions
    assert Class2.method2 in profile.functions
    assert Class1.ChildClass1.child_method_1 in profile.functions
    assert Class2.ChildClass2.child_method_2 in profile.functions


def test_profiler_warn_unwrappable():
    """
    Test for warnings when using `LineProfiler.add_*(wrap=True)` with a
    namespace which doesn't allow attribute assignment.
    """
    from line_profiler import LineProfiler

    class ProblamticMeta(type):
        def __init__(cls, *args, **kwargs):
            super(ProblamticMeta, cls).__init__(*args, **kwargs)
            cls._initialized = True

        def __setattr__(cls, attr, value):
            if not getattr(cls, '_initialized', None):
                return super(ProblamticMeta, cls).__setattr__(attr, value)
            raise AttributeError(
                f'cannot set attribute on {type(cls)} instance')

    class ProblematicClass(metaclass=ProblamticMeta):
        def method(self):
            pass

    profile = LineProfiler()
    vanilla_method = ProblematicClass.method

    with pytest.warns(match=r"cannot wrap 1 attribute\(s\) of "
                      r"<class '.*\.ProblematicClass'> \(`\{attr: value\}`\): "
                      r"\{'method': <function .*\.method at 0x.*>\}"):
        # The method is added to the profiler, but we can't assign its
        # wrapper back into the class namespace
        assert profile.add_class(ProblematicClass, wrap=True) == 1

    assert ProblematicClass.method is vanilla_method


@pytest.mark.parametrize(
    ('scoping_policy', 'add_module_targets', 'add_class_targets'),
    [('exact', {}, {'class3_method'}),
     ('children',
      {'class2_method', 'child_class2_method'},
      {'class3_method', 'child_class3_method'}),
     ('descendants',
      {'class2_method', 'child_class2_method',
       'class3_method', 'child_class3_method'},
      {'class3_method', 'child_class3_method'}),
     ('siblings',
      {'class1_method', 'child_class1_method',
       'class2_method', 'child_class2_method',
       'class3_method', 'child_class3_method', 'other_class3_method'},
      {'class3_method', 'child_class3_method', 'other_class3_method'}),
     ('none',
      {'class1_method', 'child_class1_method',
       'class2_method', 'child_class2_method',
       'class3_method', 'child_class3_method', 'other_class3_method'},
      {'child_class1_method',
       'class3_method', 'child_class3_method', 'other_class3_method'})])
def test_profiler_class_scope_matching(monkeypatch,
                                       scoping_policy,
                                       add_module_targets,
                                       add_class_targets):
    """
    Test for the class-scope-matching strategies of the
    `LineProfiler.add_*()` methods.
    """
    with ExitStack() as stack:
        stack.enter_context(restore_sys_modules())
        curdir = stack.enter_context(enter_tmpdir())

        pkg_dir = curdir / 'packages' / 'my_pkg'
        write(pkg_dir / '__init__.py')
        write(pkg_dir / 'submod1.py',
              """
        class Class1:
            def class1_method(self):
                pass

            class ChildClass1:
                def child_class1_method(self):
                    pass
              """)
        write(pkg_dir / 'subpkg2' / '__init__.py',
              """
        from ..submod1 import Class1  # Import from a sibling
        from .submod3 import Class3  # Import descendant from a child


        class Class2:
            def class2_method(self):
                pass

            class ChildClass2:
                def child_class2_method(self):
                    pass

            BorrowedChildClass = Class1.ChildClass1  # Non-sibling class
              """)
        write(pkg_dir / 'subpkg2' / 'submod3.py',
              """
        from ..submod1 import Class1


        class Class3:
            def class3_method(self):
                pass

            class OtherChildClass3:
                def child_class3_method(self):
                    pass

            # Unrelated class
            BorrowedChildClass1 = Class1.ChildClass1

        class OtherClass3:
            def other_class3_method(self):
                pass

        # Sibling class
        Class3.BorrowedChildClass3 = OtherClass3
              """)
        monkeypatch.syspath_prepend(pkg_dir.parent)

        from my_pkg import subpkg2
        from line_profiler import LineProfiler

        policies = {'func': 'none', 'class': scoping_policy,
                    'module': 'exact'}  # Don't descend into submodules
        # Add a module
        profile = LineProfiler()
        profile.add_module(subpkg2, scoping_policy=policies)
        assert len(profile.functions) == len(add_module_targets)
        added = {func.__name__ for func in profile.functions}
        assert added == set(add_module_targets)
        # Add a class
        profile = LineProfiler()
        profile.add_class(subpkg2.Class3, scoping_policy=policies)
        assert len(profile.functions) == len(add_class_targets)
        added = {func.__name__ for func in profile.functions}
        assert added == set(add_class_targets)


@pytest.mark.parametrize(
    ('scoping_policy', 'add_module_targets', 'add_subpackage_targets'),
    [('exact', {'func4'}, {'class_method'}),
     ('children', {'func4'}, {'class_method', 'func2'}),
     ('descendants', {'func4'}, {'class_method', 'func2'}),
     ('siblings', {'func4'}, {'class_method', 'func2', 'func3'}),
     ('none',
      {'func4', 'func5'},
      {'class_method', 'func2', 'func3', 'func4', 'func5'})])
def test_profiler_module_scope_matching(monkeypatch,
                                        scoping_policy,
                                        add_module_targets,
                                        add_subpackage_targets):
    """
    Test for the module-scope-matching strategies of the
    `LineProfiler.add_*()` methods.
    """
    with ExitStack() as stack:
        stack.enter_context(restore_sys_modules())
        curdir = stack.enter_context(enter_tmpdir())

        pkg_dir = curdir / 'packages' / 'my_pkg'
        write(pkg_dir / '__init__.py')
        write(pkg_dir / 'subpkg1' / '__init__.py',
              """
              import my_mod4  # Unrelated
              from .. import submod3  # Sibling
              from . import submod2  # Child


              class Class:
                  @classmethod
                  def class_method(cls):
                      pass

                  # We shouldn't descend into this no matter what
                  import my_mod5 as module
              """)
        write(pkg_dir / 'subpkg1' / 'submod2.py',
              """
              def func2():
                  pass
              """)
        write(pkg_dir / 'submod3.py',
              """
              def func3():
                  pass
              """)
        write(curdir / 'packages' / 'my_mod4.py',
              """
              import my_mod5  # Unrelated


              def func4():
                  pass
              """)
        write(curdir / 'packages' / 'my_mod5.py',
              """
              def func5():
                  pass
              """)
        monkeypatch.syspath_prepend(pkg_dir.parent)

        import my_mod4
        from my_pkg import subpkg1
        from line_profiler import LineProfiler

        policies = {'func': 'none', 'class': 'children',
                    'module': scoping_policy}
        # Add a module
        profile = LineProfiler()
        profile.add_module(my_mod4, scoping_policy=policies)
        assert len(profile.functions) == len(add_module_targets)
        added = {func.__name__ for func in profile.functions}
        assert added == set(add_module_targets)
        # Add a subpackage
        profile = LineProfiler()
        profile.add_module(subpkg1, scoping_policy=policies)
        assert len(profile.functions) == len(add_subpackage_targets)
        added = {func.__name__ for func in profile.functions}
        assert added == set(add_subpackage_targets)
        # Add a class
        profile = LineProfiler()
        profile.add_class(subpkg1.Class, scoping_policy=policies)
        assert [func.__name__ for func in profile.functions] == ['class_method']


@pytest.mark.parametrize(
    ('scoping_policy', 'add_module_targets', 'add_class_targets'),
    [('exact', {'func1'}, {'method'}),
     ('children', {'func1'}, {'method'}),
     ('descendants', {'func1', 'func2'}, {'method', 'child_class_method'}),
     ('siblings',
      {'func1', 'func2', 'func3'},
      {'method', 'child_class_method', 'func1'}),
     ('none',
      {'func1', 'func2', 'func3', 'func4'},
      {'method', 'child_class_method', 'func1', 'another_func4'})])
def test_profiler_func_scope_matching(monkeypatch,
                                      scoping_policy,
                                      add_module_targets,
                                      add_class_targets):
    """
    Test for the class-scope-matching strategies of the
    `LineProfiler.add_*()` methods.
    """
    with ExitStack() as stack:
        stack.enter_context(restore_sys_modules())
        curdir = stack.enter_context(enter_tmpdir())

        pkg_dir = curdir / 'packages' / 'my_pkg'
        write(pkg_dir / '__init__.py')
        write(pkg_dir / 'subpkg1' / '__init__.py',
              """
              from ..submod3 import func3  # Sibling
              from .submod2 import func2  # Descendant
              from my_mod4 import func4  # Unrelated

              def func1():
                  pass

              class Class:
                  def method(self):
                      pass

                  class ChildClass:
                      @classmethod
                      def child_class_method(cls):
                          pass

                  # Descendant
                  descdent_method = ChildClass.child_class_method

                  # Sibling
                  sibling_method = staticmethod(func1)

                  # Unrelated
                  from my_mod4 import another_func4 as imported_method
              """)
        write(pkg_dir / 'subpkg1' / 'submod2.py',
              """
              def func2():
                  pass
              """)
        write(pkg_dir / 'submod3.py',
              """
              def func3():
                  pass
              """)
        write(curdir / 'packages' / 'my_mod4.py',
              """
              def func4():
                  pass


              def another_func4(_):
                  pass
              """)
        monkeypatch.syspath_prepend(pkg_dir.parent)

        from my_pkg import subpkg1
        from line_profiler import LineProfiler

        policies = {'func': scoping_policy,
                    # No descensions
                    'class': 'exact', 'module': 'exact'}
        # Add a module
        profile = LineProfiler()
        profile.add_module(subpkg1, scoping_policy=policies)
        assert len(profile.functions) == len(add_module_targets)
        added = {func.__name__ for func in profile.functions}
        assert added == set(add_module_targets)
        # Add a class
        profile = LineProfiler()
        profile.add_module(subpkg1.Class, scoping_policy=policies)
        assert len(profile.functions) == len(add_class_targets)
        added = {func.__name__ for func in profile.functions}
        assert added == set(add_class_targets)


if __name__ == '__main__':
    ...
    test_simple_explicit_nonglobal_usage()
