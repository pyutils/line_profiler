import os
import subprocess
import sys
import tempfile

import pytest
import ubelt as ub


def test_single_function_autoprofile():
    """
    Test that every function in a file is profiled when autoprofile is enabled.
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())

    code = ub.codeblock(
        '''
        def func1(a):
            return a + 1

        func1(1)
        ''')
    with ub.ChDir(temp_dpath):

        script_fpath = ub.Path('script.py')
        script_fpath.write_text(code)

        args = [sys.executable, '-m', 'kernprof', '-p', 'script.py', '-l', os.fspath(script_fpath)]
        proc = ub.cmd(args)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [sys.executable, '-m', 'line_profiler', os.fspath(script_fpath) + '.lprof']
        proc = ub.cmd(args)
        raw_output = proc.stdout
        proc.check_returncode()

    assert 'func1' in raw_output
    temp_dpath.delete()


def test_multi_function_autoprofile():
    """
    Test that every function in a file is profiled when autoprofile is enabled.
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())

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

        args = [sys.executable, '-m', 'kernprof', '-p', 'script.py', '-l', os.fspath(script_fpath)]
        proc = ub.cmd(args)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [sys.executable, '-m', 'line_profiler', os.fspath(script_fpath) + '.lprof']
        proc = ub.cmd(args)
        raw_output = proc.stdout
        proc.check_returncode()

    assert 'func1' in raw_output
    assert 'func2' in raw_output
    assert 'func3' in raw_output
    assert 'func4' in raw_output

    temp_dpath.delete()


def test_duplicate_function_autoprofile():
    """
    Test that every function in a file is profiled when autoprofile is enabled.
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())

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

        args = [sys.executable, '-m', 'kernprof', '-p', 'script.py', '-l', os.fspath(script_fpath)]
        proc = ub.cmd(args)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [sys.executable, '-m', 'line_profiler', os.fspath(script_fpath) + '.lprof']
        proc = ub.cmd(args)
        raw_output = proc.stdout
        print(raw_output)
        proc.check_returncode()

    assert 'Function: func1' in raw_output
    assert 'Function: func2' in raw_output
    assert 'Function: func3' in raw_output
    assert 'Function: func4' in raw_output

    temp_dpath.delete()


def test_async_func_autoprofile():
    """
    Test the profiling of async functions when autoprofile is enabled.
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())

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
    temp_dpath.delete()

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
    (temp_dpath / 'test_mod/subpkg/__init__.py').touch()

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
    Test that every function in a file is profiled when autoprofile is enabled.
    """

    temp_dpath = ub.Path(tempfile.mkdtemp())

    script_fpath = _write_demo_module(temp_dpath)

    # args = [sys.executable, '-m', 'kernprof', '--prof-imports', '-p', 'script.py', '-l', os.fspath(script_fpath)]
    args = [sys.executable, '-m', 'kernprof', '-p', 'script.py', '-l', os.fspath(script_fpath)]
    proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
    print(proc.stdout)
    print(proc.stderr)
    proc.check_returncode()

    args = [sys.executable, '-m', 'line_profiler', os.fspath(script_fpath) + '.lprof']
    proc = ub.cmd(args, cwd=temp_dpath)
    raw_output = proc.stdout
    print(raw_output)
    proc.check_returncode()

    assert 'Function: add_one' not in raw_output
    assert 'Function: main' in raw_output


def test_autoprofile_module():
    """
    Test that every function in a file is profiled when autoprofile is enabled.
    """

    temp_dpath = ub.Path(tempfile.mkdtemp())

    script_fpath = _write_demo_module(temp_dpath)

    # args = [sys.executable, '-m', 'kernprof', '--prof-imports', '-p', 'script.py', '-l', os.fspath(script_fpath)]
    args = [sys.executable, '-m', 'kernprof', '-p', 'test_mod', '-l', os.fspath(script_fpath)]
    proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
    print(proc.stdout)
    print(proc.stderr)
    proc.check_returncode()

    args = [sys.executable, '-m', 'line_profiler', os.fspath(script_fpath) + '.lprof']
    proc = ub.cmd(args, cwd=temp_dpath)
    raw_output = proc.stdout
    print(raw_output)
    proc.check_returncode()

    assert 'Function: add_one' in raw_output
    assert 'Function: main' not in raw_output


def test_autoprofile_module_list():
    """
    Test only modules specified are autoprofiled
    """

    temp_dpath = ub.Path(tempfile.mkdtemp())

    script_fpath = _write_demo_module(temp_dpath)

    # args = [sys.executable, '-m', 'kernprof', '--prof-imports', '-p', 'script.py', '-l', os.fspath(script_fpath)]
    args = [sys.executable, '-m', 'kernprof', '-p', 'test_mod.submod1,test_mod.subpkg.submod3', '-l', os.fspath(script_fpath)]
    proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
    print(proc.stdout)
    print(proc.stderr)
    proc.check_returncode()

    args = [sys.executable, '-m', 'line_profiler', os.fspath(script_fpath) + '.lprof']
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
    temp_dpath = ub.Path(tempfile.mkdtemp())
    script_fpath = _write_demo_module(temp_dpath)

    args = [sys.executable, '-m', 'kernprof', '--prof-imports', '-p', 'test_mod.submod1', '-l', os.fspath(script_fpath)]
    proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
    print(proc.stdout)
    print(proc.stderr)
    proc.check_returncode()

    args = [sys.executable, '-m', 'line_profiler', os.fspath(script_fpath) + '.lprof']
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
    temp_dpath = ub.Path(tempfile.mkdtemp())
    script_fpath = _write_demo_module(temp_dpath)

    # import sys
    # if sys.version_info[0:2] >= (3, 11):
    #     import pytest
    #     pytest.skip('Failing due to the noop bug')

    args = [sys.executable, '-m', 'kernprof', '--prof-imports', '-p', 'script.py', '-l', os.fspath(script_fpath)]
    proc = ub.cmd(args, cwd=temp_dpath, verbose=0)
    print('Kernprof Stdout:')
    print(proc.stdout)
    print('Kernprof Stderr:')
    print(proc.stderr)
    print('About to check kernprof return code')
    proc.check_returncode()

    args = [sys.executable, '-m', 'line_profiler', os.fspath(script_fpath) + '.lprof']
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
    ['use_kernprof_exec', 'prof_mod', 'prof_imports',
     'add_one', 'add_two', 'add_operator', 'main'],
    [(False, 'test_mod.submod1', False, True, False, False, False),
     (False, 'test_mod.submod2', True, False, True, True, False),
     (False, 'test_mod', True, True, True, True, True),
     # Explicitly add all the modules via multiple `-p` flags, without
     # using the `--prof-imports` flag
     (False, ['test_mod', 'test_mod.submod1,test_mod.submod2'], False,
      True, True, True, True),
     (False, None, True, False, False, False, False),
     (True, None, True, False, False, False, False)])
def test_autoprofile_exec_package(
        use_kernprof_exec, prof_mod, prof_imports,
        add_one, add_two, add_operator, main):
    """
    Test the execution of a package.
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())
    _write_demo_module(temp_dpath)

    if use_kernprof_exec:
        args = ['kernprof']
    else:
        args = [sys.executable, '-m', 'kernprof']
    if prof_mod is not None:
        if isinstance(prof_mod, str):
            prof_mod = [prof_mod]
        for pm in prof_mod:
            args.extend(['-p', pm])
    if prof_imports:
        args.append('--prof-imports')
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

    assert ('Function: add_one' in raw_output) == add_one
    assert ('Function: add_two' in raw_output) == add_two
    assert ('Function: add_operator' in raw_output) == add_operator
    assert ('Function: _main' in raw_output) == main


@pytest.mark.parametrize(
    ['use_kernprof_exec', 'prof_mod', 'prof_imports',
     'add_one', 'add_two', 'add_four', 'add_operator', 'main'],
    [(False, 'test_mod.submod2', False, False, True, False, False, False),
     (False, 'test_mod.submod1', False, True, False, False, True, False),
     (False, 'test_mod.subpkg.submod4', True, True, True, True, True, True),
     (False, None, True, False, False, False, False, False),
     (True, None, True, False, False, False, False, False)])
def test_autoprofile_exec_module(
        use_kernprof_exec, prof_mod, prof_imports,
        add_one, add_two, add_four, add_operator, main):
    """
    Test the execution of a module.
    """
    temp_dpath = ub.Path(tempfile.mkdtemp())
    _write_demo_module(temp_dpath)

    if use_kernprof_exec:
        args = ['kernprof']
    else:
        args = [sys.executable, '-m', 'kernprof']
    if prof_mod is not None:
        args.extend(['-p', prof_mod])
    if prof_imports:
        args.append('--prof-imports')
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

    assert ('Function: add_one' in raw_output) == add_one
    assert ('Function: add_two' in raw_output) == add_two
    assert ('Function: add_four' in raw_output) == add_four
    assert ('Function: add_operator' in raw_output) == add_operator
    assert ('Function: _main' in raw_output) == main


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
        if view:
            raw_output = proc.stdout
        else:
            lp_cmd = [sys.executable, '-m', 'line_profiler', str(outfile)]
            proc = ub.cmd(lp_cmd)
            raw_output = proc.stdout
            print(raw_output)
            proc.check_returncode()

    assert ('Function: add_one' in raw_output) == prof_mod
    assert 'Function: add_two' not in raw_output
    assert ('Function: add_three' in raw_output) == prof_mod
    # If we're calling a separate process to view the results, the
    # script file will already have been deleted
    assert ('Function: main' in raw_output) == view


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
