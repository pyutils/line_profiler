import os
import re
import subprocess
import sys
import shlex
import tempfile

import pytest
import ubelt as ub


def test_single_function_autoprofile():
    """
    Test that every function in a file is profiled when autoprofile is
    enabled.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

        code = ub.codeblock(
            '''
            def func1(a):
                return a + 1

            func1(1)
            ''')
        with ub.ChDir(temp_dpath):

            script_fpath = ub.Path('script.py')
            script_fpath.write_text(code)

            args = [sys.executable, '-m', 'kernprof', '-p', 'script.py', '-l',
                    os.fspath(script_fpath)]
            proc = ub.cmd(args)
            print(proc.stdout)
            print(proc.stderr)
            proc.check_returncode()

            args = [sys.executable, '-m', 'line_profiler',
                    os.fspath(script_fpath) + '.lprof']
            proc = ub.cmd(args)
            raw_output = proc.stdout
            proc.check_returncode()

        assert 'func1' in raw_output


def test_multi_function_autoprofile():
    """
    Test that every function in a file is profiled when autoprofile is
    enabled.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

        code = ub.codeblock(
            '''
            def func1(a):
                return a + 1

            def func2(a):
                return a * 2 + 2

            def func3(a):
                return a / 10 + 3

            def func4(a):
                return a % 2 + 4

            func1(1)
            ''')
        with ub.ChDir(temp_dpath):

            script_fpath = ub.Path('script.py')
            script_fpath.write_text(code)

            args = [sys.executable, '-m', 'kernprof', '-p', 'script.py', '-l',
                    os.fspath(script_fpath)]
            proc = ub.cmd(args)
            print(proc.stdout)
            print(proc.stderr)
            proc.check_returncode()

            args = [sys.executable, '-m', 'line_profiler',
                    os.fspath(script_fpath) + '.lprof']
            proc = ub.cmd(args)
            raw_output = proc.stdout
            proc.check_returncode()

        assert 'func1' in raw_output
        assert 'func2' in raw_output
        assert 'func3' in raw_output
        assert 'func4' in raw_output


def test_duplicate_function_autoprofile():
    """
    Test that every function in a file is profiled when autoprofile is
    enabled.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

        code = ub.codeblock(
            '''
            def func1(a):
                return a + 1

            def func2(a):
                return a + 1

            def func3(a):
                return a + 1

            def func4(a):
                return a + 1

            func1(1)
            func2(1)
            func3(1)
            ''')
        with ub.ChDir(temp_dpath):

            script_fpath = ub.Path('script.py')
            script_fpath.write_text(code)

            args = [sys.executable, '-m', 'kernprof', '-p', 'script.py', '-l',
                    os.fspath(script_fpath)]
            proc = ub.cmd(args)
            print(proc.stdout)
            print(proc.stderr)
            proc.check_returncode()

            args = [sys.executable, '-m', 'line_profiler',
                    os.fspath(script_fpath) + '.lprof']
            proc = ub.cmd(args)
            raw_output = proc.stdout
            print(raw_output)
            proc.check_returncode()

        assert 'Function: func1' in raw_output
        assert 'Function: func2' in raw_output
        assert 'Function: func3' in raw_output
        assert 'Function: func4' in raw_output


def test_async_func_autoprofile():
    """
    Test the profiling of async functions when autoprofile is enabled.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

        code = ub.codeblock(
            '''
            import asyncio


            async def foo(l, x, delay=.0625):
                delay *= x
                result = (await asyncio.sleep(delay, result=x))
                l.append(result)
                return result


            async def bar():
                l = []
                coroutines = [foo(l, x) for x in range(5, -1, -1)]
                return (await asyncio.gather(*coroutines)), l


            def main(debug=None):
                (in_scheduling_order,
                 in_finishing_order) = asyncio.run(bar(), debug=debug)
                print(in_scheduling_order,  # [5, 4, 3, 2, 1, 0]
                      in_finishing_order)  # [0, 1, 2, 3, 4, 5]


            if __name__ == '__main__':
                main(debug=True)
            ''')
        with ub.ChDir(temp_dpath):

            script_fpath = ub.Path('script.py')
            script_fpath.write_text(code)

            args = [sys.executable, '-m', 'kernprof',
                    '-p', 'script.py', '-v', '-l', os.fspath(script_fpath)]
            proc = ub.cmd(args)
            raw_output = proc.stdout
            print(raw_output)
            print(proc.stderr)
            proc.check_returncode()
            assert raw_output.startswith('[5, 4, 3, 2, 1, 0] '
                                         '[0, 1, 2, 3, 4, 5]')

    assert 'Function: main' in raw_output
    assert 'Function: foo' in raw_output
    assert 'Function: bar' in raw_output


def _write_demo_module(temp_dpath):
    """
    Make a dummy test module structure
    """
    (temp_dpath / 'test_mod').ensuredir()
    (temp_dpath / 'test_mod/subpkg').ensuredir()

    (temp_dpath / 'test_mod/__init__.py').touch()
    (temp_dpath / 'test_mod/subpkg/__init__.py').write_text(ub.codeblock(
        '''
        from .submod3 import add_three
        '''))

    (temp_dpath / 'test_mod/__main__.py').write_text(ub.codeblock(
        '''
        import argparse

        from .submod1 import add_one
        from . import submod2

        def _main(args=None):
            parser = argparse.ArgumentParser()
            parser.add_argument('a', nargs='*', type=int)
            print(add_one(parser.parse_args(args).a))
            print(submod2.add_two(parser.parse_args(args).a))

        if __name__ == '__main__':
            _main()
        '''))

    (temp_dpath / 'test_mod/util.py').write_text(ub.codeblock(
        '''
        def add_operator(a, b):
            return a + b
        '''))

    (temp_dpath / 'test_mod/submod1.py').write_text(ub.codeblock(
        '''
        from test_mod.util import add_operator
        def add_one(items):
            new_items = []
            for item in items:
                new_item = add_operator(item, 1)
                new_items.append(new_item)
            return new_items
        '''))
    (temp_dpath / 'test_mod/submod2.py').write_text(ub.codeblock(
        '''
        from test_mod.util import add_operator
        def add_two(items):
            new_items = [add_operator(item, 2) for item in items]
            return new_items
        '''))
    (temp_dpath / 'test_mod/subpkg/submod3.py').write_text(ub.codeblock(
        '''
        from test_mod.util import add_operator
        def add_three(items):
            new_items = [add_operator(item, 3) for item in items]
            return new_items
        '''))
    (temp_dpath / 'test_mod/subpkg/submod4.py').write_text(ub.codeblock(
        '''
        import argparse

        from test_mod import submod1
        from ..submod2 import add_two

        def add_four(items):
            add_one = submod1.add_one
            return add_two(add_one(add_one(items)))

        def _main(args=None):
            parser = argparse.ArgumentParser()
            parser.add_argument('a', nargs='*', type=int)
            print(submod1.add_one(parser.parse_args(args).a))
            print(add_four(parser.parse_args(args).a))

        if __name__ == '__main__':
            _main()
        '''))

    script_fpath = (temp_dpath / 'script.py')
    script_fpath.write_text(ub.codeblock(
        '''
        from test_mod import submod1
        from test_mod import submod2
        from test_mod.subpkg import submod3
        import statistics

        def main():
            data = [1, 2, 3]
            val = submod1.add_one(data)
            val = submod2.add_two(val)
            val = submod3.add_three(val)

            result = statistics.harmonic_mean(val)
            print(result)

        main()
        '''))
    return script_fpath


def test_autoprofile_script_with_module():
    """
    Test that every function in a file is profiled when autoprofile is
    enabled.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

        script_fpath = _write_demo_module(temp_dpath)

        # args = [sys.executable, '-m', 'kernprof', '--prof-imports',
        #         '-p', 'script.py', '-l', os.fspath(script_fpath)]
        args = [sys.executable, '-m', 'kernprof', '-p', 'script.py', '-l',
                os.fspath(script_fpath)]
        proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [sys.executable, '-m', 'line_profiler',
                os.fspath(script_fpath) + '.lprof']
        proc = ub.cmd(args, cwd=temp_dpath)
        raw_output = proc.stdout
        print(raw_output)
        proc.check_returncode()

        assert 'Function: add_one' not in raw_output
        assert 'Function: main' in raw_output


def test_autoprofile_module():
    """
    Test that every function in a file is profiled when autoprofile is
    enabled.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

        script_fpath = _write_demo_module(temp_dpath)

        # args = [sys.executable, '-m', 'kernprof', '--prof-imports',
        #         '-p', 'script.py', '-l', os.fspath(script_fpath)]
        args = [sys.executable, '-m', 'kernprof', '-p', 'test_mod', '-l',
                os.fspath(script_fpath)]
        proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [sys.executable, '-m', 'line_profiler',
                os.fspath(script_fpath) + '.lprof']
        proc = ub.cmd(args, cwd=temp_dpath)
        raw_output = proc.stdout
        print(raw_output)
        proc.check_returncode()

    assert 'Function: add_one' in raw_output
    assert 'Function: main' not in raw_output


def test_autoprofile_module_list():
    """
    Test only modules specified are autoprofiled.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

        script_fpath = _write_demo_module(temp_dpath)

        # args = [sys.executable, '-m', 'kernprof', '--prof-imports',
        #         '-p', 'script.py', '-l', os.fspath(script_fpath)]
        args = [sys.executable, '-m', 'kernprof',
                '-p', 'test_mod.submod1,test_mod.subpkg.submod3', '-l',
                os.fspath(script_fpath)]
        proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [sys.executable, '-m', 'line_profiler',
                os.fspath(script_fpath) + '.lprof']
        proc = ub.cmd(args, cwd=temp_dpath)
        raw_output = proc.stdout
        print(raw_output)
        proc.check_returncode()

    assert 'Function: add_one' in raw_output
    assert 'Function: add_two' not in raw_output
    assert 'Function: add_three' in raw_output
    assert 'Function: main' not in raw_output


def test_autoprofile_module_with_prof_imports():
    """
    Test the imports of the specified modules are profiled as well.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
        script_fpath = _write_demo_module(temp_dpath)

        args = [sys.executable, '-m', 'kernprof', '--prof-imports',
                '-p', 'test_mod.submod1', '-l', os.fspath(script_fpath)]
        proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [sys.executable, '-m', 'line_profiler',
                os.fspath(script_fpath) + '.lprof']
        proc = ub.cmd(args, cwd=temp_dpath)
        raw_output = proc.stdout
        print(raw_output)
        proc.check_returncode()

    assert 'Function: add_one' in raw_output
    assert 'Function: add_operator' in raw_output
    assert 'Function: add_three' not in raw_output
    assert 'Function: main' not in raw_output


def test_autoprofile_script_with_prof_imports():
    """
    Test the imports of the specified modules are profiled as well.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
        script_fpath = _write_demo_module(temp_dpath)

        # import sys
        # if sys.version_info[0:2] >= (3, 11):
        #     import pytest
        #     pytest.skip('Failing due to the noop bug')

        args = [sys.executable, '-m', 'kernprof', '--prof-imports',
                '-p', 'script.py', '-l', os.fspath(script_fpath)]
        proc = ub.cmd(args, cwd=temp_dpath, verbose=0)
        print('Kernprof Stdout:')
        print(proc.stdout)
        print('Kernprof Stderr:')
        print(proc.stderr)
        print('About to check kernprof return code')
        proc.check_returncode()

        args = [sys.executable, '-m', 'line_profiler',
                os.fspath(script_fpath) + '.lprof']
        proc = ub.cmd(args, cwd=temp_dpath, verbose=0)
        raw_output = proc.stdout
        print('Line_profile Stdout:')
        print(raw_output)
        print('Line_profile Stderr:')
        print(proc.stderr)
        print('About to check line_profiler return code')
        proc.check_returncode()

    assert 'Function: add_one' in raw_output
    assert 'Function: harmonic_mean' in raw_output
    assert 'Function: main' in raw_output


@pytest.mark.parametrize(
    ('use_kernprof_exec', 'prof_mod', 'flags', 'profiled_funcs'),
    [(False, ['test_mod.submod1'], '', {'add_one', 'add_operator'}),
     # By not using `--no-preimports`, the entirety of `.submod1` is
     # passed to `add_imported_function_or_module()`
     (False, ['test_mod.submod1'], '--no-preimports', {'add_one'}),
     (False, ['test_mod.submod2'],
      '--prof-imports', {'add_two', 'add_operator'}),
     (False, ['test_mod'],
      '--prof-imports', {'add_one', 'add_two', 'add_operator', '_main'}),
     # Explicitly add all the modules via multiple `-p` flags, without
     # using the `--prof-imports` flag
     (False, ['test_mod', 'test_mod.submod1,test_mod.submod2'],
      '', {'add_one', 'add_two', 'add_operator', '_main'}),
     (False, [], '--prof-imports', set()),
     (True, [], '--prof-imports', set())])
def test_autoprofile_exec_package(use_kernprof_exec, prof_mod,
                                  flags, profiled_funcs):
    """
    Test the execution of a package.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
        _write_demo_module(temp_dpath)

        # Sanity check
        all_checked_funcs = {'add_one', 'add_two', 'add_operator', '_main'}
        profiled_funcs = set(profiled_funcs)
        assert profiled_funcs <= all_checked_funcs

        if use_kernprof_exec:
            args = ['kernprof']
        else:
            args = [sys.executable, '-m', 'kernprof']
        for pm in prof_mod:
            args.extend(['-p', pm])
        args.extend(shlex.split(flags))
        args.extend(['-l', '-m', 'test_mod', '1', '2', '3'])
        proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        prof = temp_dpath / 'test_mod.lprof'

        args = [sys.executable, '-m', 'line_profiler', os.fspath(prof)]
        proc = ub.cmd(args, cwd=temp_dpath)
        raw_output = proc.stdout
        print(raw_output)
        proc.check_returncode()

    for func in all_checked_funcs:
        assert (f'Function: {func}' in raw_output) == (func in profiled_funcs)


@pytest.mark.parametrize(
    ('use_kernprof_exec', 'prof_mod', 'flags', 'profiled_funcs'),
    [(False, 'test_mod.submod2,test_mod.subpkg.submod3.add_three',
      '--no-preimports', {'add_two'}),
     # By not using `--no-preimports`:
     # - The entirety of `.submod2` is passed to
     #   `add_imported_function_or_module()`
     # - Despite not having been imported anywhere, `add_three()` is
     #   still profiled
     (False, 'test_mod.submod2,test_mod.subpkg.submod3.add_three',
      '', {'add_two', 'add_three', 'add_operator'}),
     (False, 'test_mod.submod1', '', {'add_one', 'add_operator'}),
     (False, 'test_mod.subpkg.submod4',
      '--prof-imports',
      {'add_one', 'add_two', 'add_four', 'add_operator', '_main'}),
     (False, None, '--prof-imports', {}),
     (True, None, '--prof-imports', {}),
     # Packages are descended into by default, unless they are specified
     # with `<pkg>.__init__`
     (False, 'test_mod', '',
      {'add_one', 'add_two', 'add_three', 'add_four', 'add_operator',
       '_main'}),
     (False, 'test_mod.subpkg', '', {'add_three', 'add_four', '_main'}),
     (False, 'test_mod.subpkg.__init__', '', {'add_three'})])
def test_autoprofile_exec_module(use_kernprof_exec, prof_mod,
                                 flags, profiled_funcs):
    """
    Test the execution of a module.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
        _write_demo_module(temp_dpath)

        # Sanity check
        all_checked_funcs = {'add_one', 'add_two', 'add_three', 'add_four',
                             'add_operator', '_main'}
        profiled_funcs = set(profiled_funcs)
        assert profiled_funcs <= all_checked_funcs

        if use_kernprof_exec:
            args = ['kernprof']
        else:
            args = [sys.executable, '-m', 'kernprof']
        if prof_mod is not None:
            args.extend(['-p', prof_mod])
        args.extend(shlex.split(flags))
        args.extend(['-l', '-m', 'test_mod.subpkg.submod4', '1', '2', '3'])
        proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        prof = temp_dpath / 'test_mod.subpkg.submod4.lprof'

        args = [sys.executable, '-m', 'line_profiler', os.fspath(prof)]
        proc = ub.cmd(args, cwd=temp_dpath)
        raw_output = proc.stdout
        print(raw_output)
        proc.check_returncode()

    for func in all_checked_funcs:
        assert (f'Function: {func}' in raw_output) == (func in profiled_funcs)


@pytest.mark.parametrize('view', [True, False])
@pytest.mark.parametrize('prof_mod', [True, False])
@pytest.mark.parametrize(
    ('outfile', 'expected_outfile'),
    [(None, 'kernprof-stdin-*.lprof'),
     ('test-stdin.lprof', 'test-stdin.lprof')])
def test_autoprofile_from_stdin(
        outfile, expected_outfile, prof_mod, view) -> None:
    """
    Test the profiling of a script read from stdin.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dpath = ub.Path(tmpdir)

        kp_cmd = [sys.executable, '-m', 'kernprof', '-l']
        if prof_mod:
            kp_cmd += ['-p' 'test_mod.submod1,test_mod.subpkg.submod3']
        if outfile:
            kp_cmd += ['-o', outfile]
        if view:
            kp_cmd += ['-v']
        kp_cmd += ['-']
        with ub.ChDir(temp_dpath):
            script_fpath = _write_demo_module(ub.Path())
            proc = subprocess.run(kp_cmd,
                                  input=script_fpath.read_text(),
                                  text=True,
                                  capture_output=True)
            print(proc.stdout)
            print(proc.stderr)
            proc.check_returncode()

        outfile, = temp_dpath.glob(expected_outfile)
        lp_cmd = [sys.executable, '-m', 'line_profiler', str(outfile)]
        lp_proc = ub.cmd(lp_cmd)
        lp_proc.check_returncode()
        if view:
            raw_output = proc.stdout
        else:
            raw_output = lp_proc.stdout
            print(raw_output)

    assert ('Function: add_one' in raw_output) == prof_mod
    assert 'Function: add_two' not in raw_output
    assert ('Function: add_three' in raw_output) == prof_mod
    # If we're calling a separate process to view the results, the
    # script file will already have been deleted
    assert ('Function: main' in raw_output) == view
    # Check that `main()` is scrubbed from the written file and doesn't
    # result in spurious error messages
    assert 'Could not find file' not in lp_proc.stdout


@pytest.mark.parametrize(
    ('outfile', 'expected_outfile'),
    [(None, 'kernprof-command-*.lprof'),
     ('test-command.lprof', 'test-command.lprof')])
def test_autoprofile_from_inlined_script(outfile, expected_outfile) -> None:
    """
    Test the profiling of an inlined script (supplied with the `-c`
    flag).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dpath = ub.Path(tmpdir)

        _write_demo_module(temp_dpath)

        inlined_script = ('from test_mod import submod1, submod2; '
                          'from test_mod.subpkg import submod3; '
                          'import statistics; '
                          'data = [1, 2, 3]; '
                          'val = submod1.add_one(data); '
                          'val = submod2.add_two(val); '
                          'val = submod3.add_three(val); '
                          'result = statistics.harmonic_mean(val); '
                          'print(result);')

        kp_cmd = [sys.executable, '-m', 'kernprof',
                  '-p', 'test_mod.submod1,test_mod.subpkg.submod3', '-l']
        if outfile:
            kp_cmd += ['-o', outfile]
        kp_cmd += ['-c', inlined_script]
        proc = ub.cmd(kp_cmd, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()
        outfile, = temp_dpath.glob(expected_outfile)
        lp_cmd = [sys.executable, '-m', 'line_profiler', str(outfile)]
        proc = ub.cmd(lp_cmd)
        raw_output = proc.stdout
        print(raw_output)
        proc.check_returncode()

    assert 'Function: add_one' in raw_output
    assert 'Function: add_two' not in raw_output
    assert 'Function: add_three' in raw_output


@pytest.mark.parametrize(
    ('explicit_config', 'prof_mod', 'prof_imports', 'profiled_funcs'),
    [(True, 'test_mod.submod2', False, {'add_two'}),
     (False, None, False, {'add_one', 'add_operator'}),
     (True, 'test_mod.subpkg.submod4', None,
      {'add_one', 'add_two', 'add_four', 'add_operator', '_main'}),
     (False,
      '',  # This negates the `prof-mod` configued in the TOML file
      True, {})])
def test_autoprofile_with_customized_config(
        explicit_config, prof_mod, prof_imports, profiled_funcs):
    """
    Test autoprofiling a module with a customized TOML config file.

    TOML file
    ---------
    > [tool.line_profiler.kernprof]
    > line-by-line = true
    > prof-imports = true
    > prof-mod = ['test_mod.submod1']
    > outfile = 'my_output.lprof'
    >
    > [tool.line_profiler.cli]
    > summarize = true
    >
    > [tool.line_profiler.show.column_widths]
    > line = 8  # 2 wider than the default
    """
    docstring = test_autoprofile_with_customized_config.__doc__
    toml_content = ub.codeblock('\n'.join(
        line.lstrip('>')
        for line in (ub.codeblock(docstring).strip('\n')
                     .partition('----\n')[-1].splitlines())))
    lineno_col_width = 8

    # Sanity check
    all_checked_funcs = {'add_one', 'add_two', 'add_four',
                         'add_operator', '_main'}
    profiled_funcs = set(profiled_funcs)
    assert profiled_funcs <= all_checked_funcs

    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
        _write_demo_module(temp_dpath)
        toml = (temp_dpath / 'config.toml').absolute()
        toml.write_text(toml_content)
        prof = temp_dpath / 'my_output.lprof'

        kernprof_cmd = ['kernprof']
        lp_cmd = [sys.executable, '-m', 'line_profiler', os.fspath(prof)]
        env = os.environ.copy()
        if explicit_config:
            kernprof_cmd.append('--config=' + str(toml))
            lp_cmd.append('--config=' + str(toml))
        else:
            env['LINE_PROFILER_RC'] = str(toml)
        if prof_mod is not None:
            kernprof_cmd.extend(['-p', prof_mod])
        if prof_imports in (True, False):
            kernprof_cmd.append('--{}prof-imports'
                                .format('' if prof_imports else 'no-'))
        kernprof_cmd.extend(['-m', 'test_mod.subpkg.submod4', '1', '2', '3'])
        proc = ub.cmd(kernprof_cmd, cwd=temp_dpath, env=env, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        # `outfile` is configured by `tool.line_profiler.kernprof`
        assert prof.is_file()

        proc = ub.cmd(lp_cmd, cwd=temp_dpath, env=env)
        raw_output = proc.stdout
        print(raw_output)
        proc.check_returncode()

    for func in all_checked_funcs:
        assert (f'Function: {func}' in raw_output) == (func in profiled_funcs)
        # `summarize` is configured by `tool.line_profiler.cli`
        assert (f'- {func}' in raw_output) == (func in profiled_funcs)

    # Check the column width of the line numbers. which is configured in
    # `tool.line_profiler.show.column_widths`
    for line in raw_output.splitlines():
        if not line:
            continue
        if line.isspace():
            continue
        first, *_ = line.split()
        if not first.isdecimal():
            continue
        assert line.index(first) + len(first) == lineno_col_width


@pytest.mark.parametrize('view_in_process', [True, False])
@pytest.mark.parametrize('no_config', [True, False])
def test_autoprofile_with_no_config(no_config, view_in_process):
    """
    Test disabling config lookup with the `--no-config` flag.
    """
    toml_content = ub.codeblock('''
    [tool.line_profiler.show.column_widths]
    line = 8  # 2 wider than the default
    ''')
    lineno_col_width = 6 if no_config else 8

    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
        _write_demo_module(temp_dpath)
        toml = (temp_dpath / 'line_profiler.toml').absolute()
        toml.write_text(toml_content)
        prof = temp_dpath / 'my_output.lprof'

        kernprof_cmd = ['kernprof',
                        '-p', 'test_mod.subpkg.submod4',
                        '-o', 'my_output.lprof',
                        '-l']
        lp_cmd = [sys.executable, '-m', 'line_profiler', os.fspath(prof)]
        if view_in_process:
            kernprof_cmd.append('--view')
        if no_config:
            if view_in_process:
                kernprof_cmd.append('--no-config')
            else:
                lp_cmd.insert(-1, '--no-config')
        kernprof_cmd.extend(['-m', 'test_mod.subpkg.submod4', '1', '2', '3'])
        proc = ub.cmd(kernprof_cmd, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()
        assert prof.is_file()

        if view_in_process:
            raw_output = proc.stdout
        else:
            proc = ub.cmd(lp_cmd, cwd=temp_dpath)
            raw_output = proc.stdout
            print(raw_output)
            proc.check_returncode()

    assert 'Function: add_one' not in raw_output
    assert 'Function: add_two' not in raw_output
    assert 'Function: add_four' in raw_output
    assert 'Function: add_operator' not in raw_output
    assert 'Function: _main' in raw_output

    # Check the column width of the line numbers. which is configured in
    # `tool.line_profiler.show.column_widths`
    for line in raw_output.splitlines():
        if not line:
            continue
        if line.isspace():
            continue
        first, *_ = line.split()
        if not first.isdecimal():
            continue
        assert line.index(first) + len(first) == lineno_col_width


@pytest.mark.parametrize(
    ('prof_mod', 'profiled_funcs'),
    [('my_module',
      {'function', 'method', 'class_method', 'static_method', 'descriptor'}),
     # `function()` included in profiling via `Class.partial_method()`
     ('my_module.Class',
      {'function', 'method', 'class_method', 'static_method', 'descriptor'}),
     ('my_module.Class.descriptor', {'descriptor'})])
def test_autoprofile_callable_wrapper_objects(prof_mod, profiled_funcs):
    """
    Test that on-import profiling catches various callable-wrapper
    object types:
    - properties
    - staticmethod
    - classmethod
    - partialmethod
    Like it does regular methods and functions.
    """
    # Sanity check
    all_checked_funcs = {'function', 'method',
                         'partial_method', 'class_method', 'static_method',
                         'descriptor'}
    profiled_funcs = set(profiled_funcs)
    assert profiled_funcs <= all_checked_funcs
    # Note: `partial_method()` not to be included as its own item
    # because it's a wrapper around `function()`
    assert 'partial_method' not in profiled_funcs

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dpath = ub.Path(tmpdir)
        path = temp_dpath / 'path'
        path.mkdir()
        (path / 'my_module.py').write_text(ub.codeblock("""
        import functools


        def function(x):
            return


        class Class:
            def method(self):
                return

            @classmethod
            def class_method(cls):
                return

            @staticmethod
            def static_method():
                return

            partial_method = functools.partial(function)

            @property
            def descriptor(self):
                return
        """))
        (temp_dpath / 'script.py').write_text(ub.codeblock("""
        import my_module


        if __name__ == '__main__':
            pass
        """))

        with ub.ChDir(temp_dpath):
            args = [sys.executable, '-m', 'kernprof',
                    '-p', prof_mod, '-lv', 'script.py']
            python_path = os.environ.get('PYTHONPATH')
            if python_path:
                python_path = '{}:{}'.format(path, python_path)
            else:
                python_path = str(path)
            proc = ub.cmd(args,
                          env={**os.environ, 'PYTHONPATH': python_path},
                          verbose=2)
            raw_output = proc.stdout
        print(raw_output)
        print(proc.stderr)
        proc.check_returncode()

    for func in all_checked_funcs:
        if sys.version_info[:2] >= (3, 11) and func != 'function':
            # Match qualnames, see PR #345
            prefix = r'.*\.'
        else:
            prefix = ''
        in_output = re.search(f'^Function: {prefix}{func}',
                              raw_output,
                              re.MULTILINE)
        assert bool(in_output) == (func in profiled_funcs)
