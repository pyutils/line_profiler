from __future__ import annotations

import ast
import builtins
import contextlib
import os
import re
import shlex
import subprocess
import sys
import textwrap
import tempfile
import uuid
from collections.abc import Collection, Generator, Sequence
from types import CodeType
from typing import Any, Literal, get_args
from warnings import catch_warnings, WarningMessage

import pytest
import ubelt as ub
from line_profiler.toml_config import ConfigSource
from line_profiler.autoprofile.ast_profile_transformer import (
    AstProfileTransformer,
)
from line_profiler.autoprofile.ast_tree_profiler import AstTreeProfiler
from line_profiler.autoprofile.line_profiler_utils import add_star_import
from line_profiler.autoprofile.profmod_extractor import ProfmodExtractor
from line_profiler.line_profiler import LineStats


def test_single_function_autoprofile():
    """
    Test that every function in a file is profiled when autoprofile is
    enabled.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

        code = ub.codeblock(
            """
            def func1(a):
                return a + 1

            func1(1)
            """
        )
        with ub.ChDir(temp_dpath):
            script_fpath = ub.Path('script.py')
            script_fpath.write_text(code)

            args = [
                sys.executable,
                '-m',
                'kernprof',
                '-p',
                'script.py',
                '-l',
                os.fspath(script_fpath),
            ]
            proc = ub.cmd(args)
            print(proc.stdout)
            print(proc.stderr)
            proc.check_returncode()

            args = [
                sys.executable,
                '-m',
                'line_profiler',
                os.fspath(script_fpath) + '.lprof',
            ]
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
            """
            def func1(a):
                return a + 1

            def func2(a):
                return a * 2 + 2

            def func3(a):
                return a / 10 + 3

            def func4(a):
                return a % 2 + 4

            func1(1)
            """
        )
        with ub.ChDir(temp_dpath):
            script_fpath = ub.Path('script.py')
            script_fpath.write_text(code)

            args = [
                sys.executable,
                '-m',
                'kernprof',
                '-p',
                'script.py',
                '-l',
                os.fspath(script_fpath),
            ]
            proc = ub.cmd(args)
            print(proc.stdout)
            print(proc.stderr)
            proc.check_returncode()

            args = [
                sys.executable,
                '-m',
                'line_profiler',
                os.fspath(script_fpath) + '.lprof',
            ]
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
            """
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
            """
        )
        with ub.ChDir(temp_dpath):
            script_fpath = ub.Path('script.py')
            script_fpath.write_text(code)

            args = [
                sys.executable,
                '-m',
                'kernprof',
                '-p',
                'script.py',
                '-l',
                os.fspath(script_fpath),
            ]
            proc = ub.cmd(args)
            print(proc.stdout)
            print(proc.stderr)
            proc.check_returncode()

            args = [
                sys.executable,
                '-m',
                'line_profiler',
                os.fspath(script_fpath) + '.lprof',
            ]
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
            """
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
            """
        )
        with ub.ChDir(temp_dpath):
            script_fpath = ub.Path('script.py')
            script_fpath.write_text(code)

            args = [
                sys.executable,
                '-m',
                'kernprof',
                '-p',
                'script.py',
                '-v',
                '-l',
                os.fspath(script_fpath),
            ]
            proc = ub.cmd(args)
            raw_output = proc.stdout
            print(raw_output)
            print(proc.stderr)
            proc.check_returncode()
            assert raw_output.startswith(
                '[5, 4, 3, 2, 1, 0] [0, 1, 2, 3, 4, 5]'
            )

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
    (temp_dpath / 'test_mod/subpkg/__init__.py').write_text(
        ub.codeblock(
            """
        from .submod3 import add_three
        """
        )
    )

    (temp_dpath / 'test_mod/__main__.py').write_text(
        ub.codeblock(
            """
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
        """
        )
    )

    (temp_dpath / 'test_mod/util.py').write_text(
        ub.codeblock(
            """
        def add_operator(a, b):
            return a + b
        """
        )
    )

    # Note: this can't be profiled because `test_mod.more-utils` is not
    # a valid dotted path
    (temp_dpath / 'test_mod/dev-utils.py').write_text(
        ub.codeblock(
            '''
        """
        Just imagine that the file contains some dev tools.
        """

        def publish_pkg():
            pass
        '''
        )
    )

    (temp_dpath / 'test_mod/submod1.py').write_text(
        ub.codeblock(
            """
        from test_mod.util import add_operator
        def add_one(items):
            new_items = []
            for item in items:
                new_item = add_operator(item, 1)
                new_items.append(new_item)
            return new_items
        """
        )
    )
    (temp_dpath / 'test_mod/submod2.py').write_text(
        ub.codeblock(
            """
        from test_mod.util import add_operator
        def add_two(items):
            new_items = [add_operator(item, 2) for item in items]
            return new_items
        """
        )
    )
    (temp_dpath / 'test_mod/subpkg/submod3.py').write_text(
        ub.codeblock(
            """
        from test_mod.util import add_operator
        def add_three(items):
            new_items = [add_operator(item, 3) for item in items]
            return new_items
        """
        )
    )
    (temp_dpath / 'test_mod/subpkg/submod4.py').write_text(
        ub.codeblock(
            """
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
        """
        )
    )

    script_fpath = temp_dpath / 'script.py'
    script_fpath.write_text(
        ub.codeblock(
            """
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
        """
        )
    )
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
        args = [
            sys.executable,
            '-m',
            'kernprof',
            '-p',
            'script.py',
            '-l',
            os.fspath(script_fpath),
        ]
        proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [
            sys.executable,
            '-m',
            'line_profiler',
            os.fspath(script_fpath) + '.lprof',
        ]
        proc = ub.cmd(args, cwd=temp_dpath)
        raw_output = proc.stdout
        print(raw_output)
        proc.check_returncode()

        assert 'Function: add_one' not in raw_output
        assert 'Function: main' in raw_output


@pytest.mark.parametrize('static_resolution', [True, False])
def test_autoprofile_module(static_resolution):
    """
    Test that every function in a file is profiled when autoprofile is
    enabled.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)

        script_fpath = _write_demo_module(temp_dpath)

        args = [
            sys.executable,
            '-m',
            'kernprof',
            '-p',
            'test_mod',
            '-l',
            os.fspath(script_fpath),
        ]
        env = dict(os.environ)
        if static_resolution:
            env['LINE_PROFILER_STATIC_ANALYSIS'] = '1'
        else:
            env.pop('LINE_PROFILER_STATIC_ANALYSIS', None)
        proc = ub.cmd(args, cwd=temp_dpath, env=env, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [
            sys.executable,
            '-m',
            'line_profiler',
            os.fspath(script_fpath) + '.lprof',
        ]
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
        args = [
            sys.executable,
            '-m',
            'kernprof',
            '-p',
            'test_mod.submod1,test_mod.subpkg.submod3',
            '-l',
            os.fspath(script_fpath),
        ]
        proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [
            sys.executable,
            '-m',
            'line_profiler',
            os.fspath(script_fpath) + '.lprof',
        ]
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

        args = [
            sys.executable,
            '-m',
            'kernprof',
            '--prof-imports',
            '-p',
            'test_mod.submod1',
            '-l',
            os.fspath(script_fpath),
        ]
        proc = ub.cmd(args, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [
            sys.executable,
            '-m',
            'line_profiler',
            os.fspath(script_fpath) + '.lprof',
        ]
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

        args = [
            sys.executable,
            '-m',
            'kernprof',
            '--prof-imports',
            '-p',
            'script.py',
            '-l',
            os.fspath(script_fpath),
        ]
        proc = ub.cmd(args, cwd=temp_dpath, verbose=0)
        print('Kernprof Stdout:')
        print(proc.stdout)
        print('Kernprof Stderr:')
        print(proc.stderr)
        print('About to check kernprof return code')
        proc.check_returncode()

        args = [
            sys.executable,
            '-m',
            'line_profiler',
            os.fspath(script_fpath) + '.lprof',
        ]
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
    [
        (False, ['test_mod.submod1'], '', {'add_one', 'add_operator'}),
        # By not using `--no-preimports`, the entirety of `.submod1` is
        # passed to `add_imported_function_or_module()`
        (False, ['test_mod.submod1'], '--no-preimports', {'add_one'}),
        (
            False,
            ['test_mod.submod2'],
            '--prof-imports',
            {'add_two', 'add_operator'},
        ),
        (
            False,
            ['test_mod'],
            '--prof-imports',
            {'add_one', 'add_two', 'add_operator', '_main'},
        ),
        # Explicitly add all the modules via multiple `-p` flags, without
        # using the `--prof-imports` flag
        (
            False,
            ['test_mod', 'test_mod.submod1,test_mod.submod2'],
            '',
            {'add_one', 'add_two', 'add_operator', '_main'},
        ),
        (False, [], '--prof-imports', set()),
        (True, [], '--prof-imports', set()),
    ],
)
def test_autoprofile_exec_package(
    use_kernprof_exec, prof_mod, flags, profiled_funcs
):
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
    [
        (
            False,
            'test_mod.submod2,test_mod.subpkg.submod3.add_three',
            '--no-preimports',
            {'add_two'},
        ),
        # By not using `--no-preimports`:
        # - The entirety of `.submod2` is passed to
        #   `add_imported_function_or_module()`
        # - Despite not having been imported anywhere, `add_three()` is
        #   still profiled
        (
            False,
            'test_mod.submod2,test_mod.subpkg.submod3.add_three',
            '',
            {'add_two', 'add_three', 'add_operator'},
        ),
        (False, 'test_mod.submod1', '', {'add_one', 'add_operator'}),
        (
            False,
            'test_mod.subpkg.submod4',
            '--prof-imports',
            {'add_one', 'add_two', 'add_four', 'add_operator', '_main'},
        ),
        (False, None, '--prof-imports', {}),
        (True, None, '--prof-imports', {}),
        # Packages are descended into by default, unless they are specified
        # with `<pkg>.__init__`
        (
            False,
            'test_mod',
            '',
            {
                'add_one',
                'add_two',
                'add_three',
                'add_four',
                'add_operator',
                '_main',
            },
        ),
        (False, 'test_mod.subpkg', '', {'add_three', 'add_four', '_main'}),
        (False, 'test_mod.subpkg.__init__', '', {'add_three'}),
    ],
)
def test_autoprofile_exec_module(
    use_kernprof_exec, prof_mod, flags, profiled_funcs
):
    """
    Test the execution of a module.
    """
    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
        _write_demo_module(temp_dpath)

        # Sanity check
        all_checked_funcs = {
            'add_one',
            'add_two',
            'add_three',
            'add_four',
            'add_operator',
            '_main',
        }
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
    [
        (None, 'kernprof-stdin-*.lprof'),
        ('test-stdin.lprof', 'test-stdin.lprof'),
    ],
)
def test_autoprofile_from_stdin(
    outfile, expected_outfile, prof_mod, view
) -> None:
    """
    Test the profiling of a script read from stdin.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dpath = ub.Path(tmpdir)

        kp_cmd = [sys.executable, '-m', 'kernprof', '-l']
        if prof_mod:
            kp_cmd += ['-ptest_mod.submod1,test_mod.subpkg.submod3']
        if outfile:
            kp_cmd += ['-o', outfile]
        if view:
            kp_cmd += ['-v']
        kp_cmd += ['-']
        with ub.ChDir(temp_dpath):
            script_fpath = _write_demo_module(ub.Path())
            proc = subprocess.run(
                kp_cmd,
                input=script_fpath.read_text(),
                text=True,
                capture_output=True,
            )
            print(proc.stdout)
            print(proc.stderr)
            proc.check_returncode()

        (outfile,) = temp_dpath.glob(expected_outfile)
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
    [
        (None, 'kernprof-command-*.lprof'),
        ('test-command.lprof', 'test-command.lprof'),
    ],
)
def test_autoprofile_from_inlined_script(outfile, expected_outfile) -> None:
    """
    Test the profiling of an inlined script (supplied with the `-c`
    flag).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dpath = ub.Path(tmpdir)

        _write_demo_module(temp_dpath)

        inlined_script = (
            'from test_mod import submod1, submod2; '
            'from test_mod.subpkg import submod3; '
            'import statistics; '
            'data = [1, 2, 3]; '
            'val = submod1.add_one(data); '
            'val = submod2.add_two(val); '
            'val = submod3.add_three(val); '
            'result = statistics.harmonic_mean(val); '
            'print(result);'
        )

        kp_cmd = [
            sys.executable,
            '-m',
            'kernprof',
            '-p',
            'test_mod.submod1,test_mod.subpkg.submod3',
            '-l',
        ]
        if outfile:
            kp_cmd += ['-o', outfile]
        kp_cmd += ['-c', inlined_script]
        proc = ub.cmd(kp_cmd, cwd=temp_dpath, verbose=2)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()
        (outfile,) = temp_dpath.glob(expected_outfile)
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
    [
        (True, 'test_mod.submod2', False, {'add_two', 'add_operator'}),
        (False, None, False, {'add_one', 'add_operator'}),
        (
            True,
            'test_mod.subpkg.submod4',
            None,
            {'add_one', 'add_two', 'add_four', 'add_operator', '_main'},
        ),
        (
            False,
            '',  # This negates the `prof-mod` configued in the TOML file
            True,
            {},
        ),
    ],
)
def test_autoprofile_with_customized_config(
    explicit_config, prof_mod, prof_imports, profiled_funcs
):
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
    toml_content = ub.codeblock(
        '\n'.join(
            line.lstrip('>')
            for line in (
                ub.codeblock(docstring)
                .strip('\n')
                .partition('----\n')[-1]
                .splitlines()
            )
        )
    )
    lineno_col_width = 8

    # Sanity check
    all_checked_funcs = {
        'add_one',
        'add_two',
        'add_four',
        'add_operator',
        '_main',
    }
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
            kernprof_cmd.append(
                '--{}prof-imports'.format('' if prof_imports else 'no-')
            )
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
    toml_content = ub.codeblock("""
    [tool.line_profiler.show.column_widths]
    line = 8  # 2 wider than the default
    """)
    lineno_col_width = 6 if no_config else 8

    with tempfile.TemporaryDirectory() as tmp:
        temp_dpath = ub.Path(tmp)
        _write_demo_module(temp_dpath)
        toml = (temp_dpath / 'line_profiler.toml').absolute()
        toml.write_text(toml_content)
        prof = temp_dpath / 'my_output.lprof'

        kernprof_cmd = [
            'kernprof',
            '-p',
            'test_mod.subpkg.submod4',
            '-o',
            'my_output.lprof',
            '-l',
        ]
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
    [
        (
            'my_module',
            {
                'function',
                'method',
                'class_method',
                'static_method',
                'descriptor',
            },
        ),
        # `function()` included in profiling via `Class.partial_method()`
        (
            'my_module.Class',
            {
                'function',
                'method',
                'class_method',
                'static_method',
                'descriptor',
            },
        ),
        ('my_module.Class.descriptor', {'descriptor'}),
    ],
)
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
    all_checked_funcs = {
        'function',
        'method',
        'partial_method',
        'class_method',
        'static_method',
        'descriptor',
    }
    profiled_funcs = set(profiled_funcs)
    assert profiled_funcs <= all_checked_funcs
    # Note: `partial_method()` not to be included as its own item
    # because it's a wrapper around `function()`
    assert 'partial_method' not in profiled_funcs

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dpath = ub.Path(tmpdir)
        path = temp_dpath / 'path'
        path.mkdir()
        (path / 'my_module.py').write_text(
            ub.codeblock("""
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
        """)
        )
        (temp_dpath / 'script.py').write_text(
            ub.codeblock("""
        import my_module


        if __name__ == '__main__':
            pass
        """)
        )

        with ub.ChDir(temp_dpath):
            args = [
                sys.executable,
                '-m',
                'kernprof',
                '-p',
                prof_mod,
                '-lv',
                'script.py',
            ]
            python_path = os.environ.get('PYTHONPATH')
            if python_path:
                python_path = '{}:{}'.format(path, python_path)
            else:
                python_path = str(path)
            proc = ub.cmd(
                args, env={**os.environ, 'PYTHONPATH': python_path}, verbose=2
            )
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
        in_output = re.search(
            f'^Function: {prefix}{func}', raw_output, re.MULTILINE
        )
        assert bool(in_output) == (func in profiled_funcs)


@pytest.mark.parametrize(
    ('prof_mod', 'prof_imports',
     'expected_profiled_funcs', 'expected_output', 'expect_warning',
     'script_args'),
    [
        # When no `--prof-mod` targets are supplied, the imported module
        # is profiled iff `--prof-imports` and `--prof-star-imports`,
        # and the profiling hook is inserted by `AstProfileTransformer`
        ([], True, ['make_unnumbered_list', 'make_numbered_list'],
         '* 1\n* 2\n* 3', True, ['1', '2', '3']),
        ([], False, [], '* 1\n* 2\n* 3', False, ['1', '2', '3']),
        # With an appropriate `--prof-mod`, the target is profiled if
        # `--prof-star-imports`; the profiling hook is inserted by
        # `AstTreeProfiler` using `ProfmodExtractor.extract_all()`
        (['__module__.make_numbered_list', '__module__.bad_target'], False,
         ['make_numbered_list'], '1. a\n2. b\n3. c', True,
         ['-n', 'a', 'b', 'c']),
        (['__module__', 'bad_module'], False,
         ['make_unnumbered_list', 'make_numbered_list'],
         '1. a\n2. b\n3. c', True, ['-n', 'a', 'b', 'c']),
        (['bad_module'], False, [],
         '1. a\n2. b\n3. c', False, ['-n', 'a', 'b', 'c']),
    ])
def test_autoprofile_star_imports(
    prof_mod: Sequence[str],
    prof_imports: bool,
    expected_profiled_funcs: Collection[
        Literal['make_unnumbered_list', 'make_numbered_list']
    ],
    expected_output: str,
    expect_warning: bool,
    script_args: Sequence[str],
) -> None:
    """
    Test that the ``--prof-star-imports`` CLI flag in :py:mod:`kernprof`
    works as intended.
    """
    module_name = next(_propose_module_names())
    imported_module = ub.codeblock(r"""
    from __future__ import annotations

    from collections.abc import Sequence
    from textwrap import indent
    from typing import Any


    __all__ = ('make_unnumbered_list', 'make_numbered_list')


    def make_unnumbered_list(items: Sequence[Any], bullet: str = '*') -> str:
        return indent('\n'.join(str(item) for item in items), bullet + ' ')


    def make_numbered_list(items: Sequence[Any]) -> str:
        width = len(str(len(items)))
        return '\n'.join(
            f'{index:>{width}}. {item}' for index, item in enumerate(items, 1)
        )
    """).strip('\n')
    test_code = ub.codeblock(f"""
    from __future__ import annotations

    from argparse import ArgumentParser
    from collections.abc import Callable, Sequence
    from typing import Any

    from {module_name} import *


    def main(args: Sequence[str] | None = None) -> None:
        parser = ArgumentParser()
        parser.add_argument('-n', '--numbered', action='store_true')
        parser.add_argument('args', nargs='+')
        arguments = parser.parse_args(args)
        if arguments.numbered:
            func: Callable[[Sequence[Any]], str] = make_numbered_list
        else:
            func = make_unnumbered_list
        print(func(arguments.args))


    if __name__ == '__main__':
        main()
    """).strip('\n')

    cmd = [sys.executable, '-m', 'kernprof']
    kernprof_options = ['-lzv', '--no-preimports']
    script_options = ['-c', test_code, *script_args]
    if prof_imports:
        kernprof_options.append('--prof-imports')
    if prof_mod:
        prof_mod = [
            target.replace('__module__', module_name) for target in prof_mod
        ]
        kernprof_options.extend(['--prof-mod', ','.join(prof_mod)])

    with contextlib.ExitStack() as stack:
        tmp = ub.Path(stack.enter_context(tempfile.TemporaryDirectory()))
        python_path = tmp / 'python-path'
        python_path.mkdir()
        (python_path / (module_name + '.py')).write_text(imported_module)
        stats_path = tmp / 'out.lprof'

        kernprof_options.extend(['--outfile', str(stats_path)])

        mp = stack.enter_context(pytest.MonkeyPatch.context())
        mp.setenv('PYTHONPATH', str(python_path), prepend=os.pathsep)

        # For convenience, instead of using parametrization for the
        # presence of the `--prof-star-imports` flag, just test both
        # cases in a loop;
        # the supplied `expected_profiled_funcs` is an upper bound, and
        # those two functions are never profiled if the option is false
        for prof_star_imports, expected_funcs in [
            (True, expected_profiled_funcs), (False, []),
        ]:
            kp_options = kernprof_options.copy()
            if prof_star_imports:
                kp_options.append('--prof-star-imports')
            else:
                kp_options.append('--no-prof-star-imports')
            proc = ub.cmd(
                cmd + kp_options + script_options, check=True, verbose=2,
            )
            assert isinstance(proc.stdout, str)
            assert expected_output in proc.stdout
            # `-c` code always profiled
            assert 'def main' in proc.stdout

            if not prof_star_imports:  # Warning may be issued
                streams = [proc.stdout]
                if isinstance(proc.stderr, str):
                    streams.append(proc.stderr)

                warning = (
                    'UserWarning:.* 1 .* target.* dropped.* '
                    r'star-imports .*\bnot profiled'
                )
                assert (
                    any(re.search(warning, stream) for stream in streams)
                    == expect_warning
                )

            assert stats_path.exists()
            stats = LineStats.from_files(stats_path)
            stats_path.unlink()

            func_names = {func for *_, func in stats.timings}
            for func in 'make_unnumbered_list', 'make_numbered_list':
                assert (func in func_names) == (func in expected_funcs)


@pytest.mark.parametrize(
    ('expected_output', 'script_args'),
    [('foo.jam.qux = True', ['foo', 'jam', 'qux'])])
@pytest.mark.parametrize(
    ('prof_mod', 'prof_imports', 'prof_nested_imports',
     'expected_profiled_funcs', 'expect_warning'),
    [
        # By default, we profile imports as long as they aren't inside
        # function definitions or loops
        ([], True, [], ['loads', 'parse_args'], None),
        ([], True, ['all'], ['loads', 'parse_args', 'dedent'], None),
        # As with `--prof-mod`, `--prof-nested-imports` defaults (and
        # previously-specified values) can be invalidated by passing an
        # empty string
        ([], True, [''], [], None),
        ([], True, ['func-defs', ''], [], None),
        # Check that we're warning against bad values
        ([], True, ['', 'bad-value'], [], r"\binvalid\b.* \['bad-value'\]"),
        # Also test cases where we use a combination of `--prof-mod`
        # and `--prof-nested-imports` to narrow down the profiled funcs
        (['argparse.ArgumentParser', 'textwrap.dedent'], False,
         [], ['parse_args'], None),
        (['argparse.ArgumentParser', 'textwrap.dedent'], False,
         ['all'], ['parse_args', 'dedent'], None),
        (['argparse.ArgumentParser', 'textwrap.dedent'], False,
         ['try-except'], [], None),
    ])
def test_autoprofile_nested_imports(
    prof_mod: Sequence[str],
    prof_imports: bool,
    prof_nested_imports: Sequence[str],
    expected_profiled_funcs: Collection[
        Literal['loads', 'parse_args', 'dedent']
    ],
    expected_output: str,
    expect_warning: str | None,
    script_args: Sequence[str],
) -> None:
    """
    Test that the ``--prof-nested-imports`` CLI flag in
    :py:mod:`kernprof` works as intended.
    """
    sample_toml = r"""
    [foo]
    spam = 1
    ham = [1, 2, 3]

    [foo.eggs]
    foobar = 'some string'
    baz = 'some other string'

    [foo.jam]
    qux = true

    [bar]
    foobar = []
    """
    test_code_template = ub.codeblock("""
    from __future__ import annotations

    try:
        from tomllib import loads as load_toml
    except ImportError:  # Python < 3.11
        from tomli import loads as load_toml

    if True:
        from argparse import ArgumentParser
        from collections.abc import Callable, Sequence


    def main(args: Sequence[str] | None = None) -> None:
        from textwrap import dedent

        with open({}) as fobj:
            text = dedent(fobj.read())

        data = load_toml(text)

        parser = ArgumentParser()
        parser.add_argument('keys', nargs='*')
        keys = parser.parse_args(args).keys

        for key in keys:
            data = data[key]
        print('.'.join(keys), '=', data)


    if __name__ == '__main__':
        main()
    """).strip('\n')

    cmd = [sys.executable, '-m', 'kernprof']
    kernprof_options = ['-lzv', '--no-preimports']
    if prof_imports:
        kernprof_options.append('--prof-imports')
    if prof_mod:
        kernprof_options.extend(['--prof-mod', ','.join(prof_mod)])
    for pni in prof_nested_imports:
        kernprof_options.extend(['--prof-nested-imports', pni])

    func_aliases: dict[str, str] = {}
    if hasattr(CodeType, 'co_qualname'):
        # Since Python 3.11 we use the qualname of the function in
        # outputs
        func_aliases['parse_args'] = 'ArgumentParser.parse_args'

    with tempfile.TemporaryDirectory() as tmp_:
        tmp = ub.Path(tmp_)
        toml_path = tmp / 'data.toml'
        stats_path = tmp / 'out.lprof'
        toml_path.write_text(sample_toml)

        kernprof_options.extend(['--outfile', str(stats_path)])
        script_options = [
            '-c', test_code_template.format(repr(str(toml_path))),
            *script_args
        ]

        proc = ub.cmd(
            cmd + kernprof_options + script_options, check=True, verbose=2,
        )
        assert isinstance(proc.stdout, str)
        assert expected_output in proc.stdout
        # `-c` code always profiled
        assert 'def main' in proc.stdout

        streams = [proc.stdout]
        if isinstance(proc.stderr, str):
            streams.append(proc.stderr)
        warning = 'UserWarning:.*--prof-nested-imports'
        if expect_warning is None:
            assert not any(re.search(warning, stream) for stream in streams)
        else:
            warning = f'{warning}.*{expect_warning}'
            assert any(re.search(warning, stream) for stream in streams)

        assert stats_path.exists()
        stats = LineStats.from_files(stats_path)
        stats_path.unlink()

        func_names = {func for *_, func in stats.timings}
        for func in [
            'loads',  # In a try-except block
            'parse_args',  # In an if block
            'dedent',  # In a function def (not profiled by default)
        ]:
            profiled = func_aliases.get(func, func) in func_names
            assert profiled == (func in expected_profiled_funcs)


@pytest.mark.parametrize(
    ('prof_mod', 'prof_os', 'prof_minidom', 'prof_pulldom',
     'prof_elem', 'prof_etree', 'prof_parser'),
    # Trivial cases
    [([], False, False, False, False, False, False),
     (['os', 'foo.bar'], True, False, False, False, False, False),
     (['xml.etree.ElementTree'], False, False, False, True, True, True),
     (['xml.etree.ElementTree.Element', 'xml.etree.ElementTree.XMLParser',
       'xml.dom.minidom'],
      False, True, False, True, False, True)])
def test_multitarget_import_resolution(
    prof_mod: list[str],
    prof_os: bool,
    prof_minidom: bool, prof_pulldom: bool,
    prof_elem: bool, prof_etree: bool, prof_parser: bool,
) -> None:
    """
    Test that (from-)import statements with multiple targets are
    correctly transformed by :py:class:`.AstTreeProfiler`, resolving to
    the correct entities being profiled.

    See also:
        Issue #433
    """
    input_module = ub.codeblock(
        """
        import os
        from xml.dom import minidom, pulldom
        from xml.etree.ElementTree import (
            Element, ElementTree, XMLParser,
        )


        if __name__ == '__main__':
            pass
        """,
    )
    with tempfile.TemporaryDirectory() as tmp:
        fpath = ub.Path(tmp) / 'script.py'
        fpath.write_text(input_module)
        module_ast = AstTreeProfiler(str(fpath), prof_mod, False).profile()
    output_module = ast.unparse(module_ast)

    for target, profiled in {
        'os': prof_os,
        'minidom': prof_minidom, 'pulldom': prof_pulldom,
        'Element': prof_elem, 'ElementTree': prof_etree,
        'XMLParser': prof_parser,
    }.items():
        pattern = rf'add_imported_function_or_module\({target}\)'
        assert bool(re.search(pattern, output_module)) == profiled


@pytest.mark.parametrize(
    ('prof_mod', 'expected', 'method', 'expect_warning'),
    [(['foo', 'foobar.ham'], {0: ['foo'], 2: ['ham']}, 'extract_all', None),
     (['baz', 'foobar'], {1: ['baz'], 2: ['spam', 'ham', 'jam']},
      'extract_all', None),
     (['foobar', 'qux', 'fred'],
      {2: ['spam', 'ham', 'jam'], 3: ['ham'], 5: ['alexa', 'bob']},
      'extract_all', None),
     # `from quux import *` cannot be profiled as of now, and results in
     # a warning
     (['qux', 'quux'], {3: ['ham']}, 'extract_all',
      r'1 .* target.* dropped .* import \*.*'
      r'- line 5: \* \(from quux\)'),
     # This doesn't result in a `UserWarning` for dropped targets,
     # because there is only one selected target on the multi-target
     # import line
     (['foo', 'foobar.ham'], {0: 'foo', 2: 'ham'}, 'run', None),
     # This however results in the warning that `spam` and `ham` are
     # supposed to be profiled, but are dropped
     (['baz', 'foobar'], {1: 'baz', 2: 'jam'}, 'run',
      '2 .* target.* dropped .* multi-target.*'
      r'- line 3: ham \(= foobar.ham\), spam \(= foobar.spam\)'),
     # - Despite how `foobar.ham` is shadowed by `qux.ham`, the former
     #   is still dropped profiling; and so we include that in the
     #   warning message, and also indicate the name's source
     # - Note that the warning message is multiline because there are
     #   dropped imports on multiple lines
     (['foobar', 'qux', 'fred'], {2: 'jam', 3: 'ham', 5: 'bob'}, 'run',
      '3 .* target.* dropped .* multi-target.*'
      r'\n- line 3: ham \(= foobar.ham\), spam \(= foobar.spam\)'
      r'\n- line 6: alexa \(= fred.alice\)'),
     # Same warning for `quux.*` as above for `.extract_all()`
     (['qux', 'quux'], {3: 'ham'}, 'run',
      r'1 .* target.* dropped .* import \*.*'
      r'- line 5: \* \(from quux\)')])
def test_profmod_extractor_multitarget_behavior(
    prof_mod: list[str],
    expected: dict[int, str] | dict[int, list[str]],
    method: Literal['extract_all', 'run'],
    expect_warning: str | None,
) -> None:
    """
    Test that :py:meth:`.ProfmodExtractor.extract_all` and
    :py:meth:`.ProfmodExtractor.run` behaves as expected:

    ``.run()`` (legacy method):

        - Returns ``dict[int, str]``, where ``str`` is the last target
          in a (multi-target) import(-from) statement

        - Issues a :py:class:`DeprecationWarning` urging users to use
          :py:meth:`.ProfmodExtractor.extract_all` instead

        - Issues a :py:class:`UserWarning` against dropped profiling
          targets because of multi-target import statements (if any)

        - Issues a :py:class:`UserWarning` against dropped profiling
          targets because of the currently unsupported
          ``from ... import *`` statements (if any)

    ``.extract_all()`` (new method):

        - Returns \
``dict[tuple[Literal['body'], int], list[ImportTarget]]``,
          where ``ImportTarget.resolved_name`` is the name of the import
          target in the namespace

        - Does not result in the above warnings, except for the
          ``from ... import *`` case

    See also:
        Issue #433
    """
    code = ub.codeblock(
        """
        import foo, bar
        import baz
        from foobar import spam, ham, eggs as jam
        from qux import ham  # This shadows `foobar.ham` above
        from quux import *  # Star-imports ignored for now
        from fred import alice as alexa, bob


        def func() -> None:
            pass
        """,
    ).strip('\n')
    depr_warning_pattern = 'run.* deprecated.* use .*extract_all'
    targets_warning_pattern = 'profiling target.* dropped.*'
    warnings: list[WarningMessage]
    checks: list[tuple[bool, str, type[Warning]]] = []
    # Check that the deprecation warning is only issued when using
    # `.run()`
    checks.append((method == 'run', depr_warning_pattern, DeprecationWarning))
    # Check that the user warnin is only issued when a target has been
    # dropped (and not shadowed by a later import)
    if expect_warning:
        checks.append((True, expect_warning, UserWarning))
    else:
        checks.append((False, targets_warning_pattern, UserWarning))

    with contextlib.ExitStack() as stack:
        tmpdir = stack.enter_context(tempfile.TemporaryDirectory())
        fname = ub.Path(tmpdir) / 'script.py'
        fname.write_text(code)

        warnings = stack.enter_context(catch_warnings(record=True))
        extractor = ProfmodExtractor(ast.parse(code), str(fname), prof_mod)
        if method == 'run':
            assert extractor.run() == expected
        else:
            result: dict[int, list[str | None]] = {}
            for loc, imports in extractor.extract_all().items():
                # XXX: these assertions are true for the time being, but
                # will become false when we extend to non-top-level
                # import statements
                assert len(loc) == 2
                assert loc[0] == 'body'
                assert isinstance(loc[1], int)
                result[loc[1]] = [imp.resolved_name for imp in imports]
            assert result == expected

    _check_warnings(warnings, checks)


def _check_warnings(
    warnings: Sequence[WarningMessage],
    checks: Collection[tuple[bool, str, type[Warning]]],
) -> None:
    """
    With each tuple of ``warning_expected, msg, WarningType``, check
    ``warnings`` that:

    - If ``warning_expected = True``, there is at least 1 matching
      warning.

    - If ``warning_expected = False``, thers is no matching warning.
    """
    for warning_expected, pattern, WarningType in checks:
        regex = re.compile(pattern)
        matches = [
            msg for msg in warnings
            if issubclass(msg.category, WarningType)
            if regex.search(str(msg.message))
        ]
        if bool(matches) == warning_expected:
            continue
        if warning_expected:
            # Note: Until Python 3.14 `WarningMessage.__repr__()` is
            # terse; use `.__str__()` to show more context
            raise AssertionError(
                f'expected {WarningType.__name__} matching {pattern!r}, '
                f'didn\'t get a match out of {len(warnings)} '
                f'warnings captured: {[str(m) for m in warnings]!r}'
            )
        else:
            raise AssertionError(
                f'expected no {WarningType.__name__} matching {pattern!r}, '
                f'got {len(matches)} match(es): {[str(m) for m in matches]!r}'
            )


class _RecordingProfiler:
    """
    Mock :py:class:`line_profiler.LineProfiler` object.
    """
    def __init__(self) -> None:
        self.profiled_objects: list[Any] = []

    def __call__(self, x: Any) -> Any:
        return x

    def add_imported_function_or_module(self, obj) -> Literal[1]:
        self.profiled_objects.append(obj)
        return 1


def test_multitarget_import_transformation_executes() -> None:
    """
    Test the runtime behavior of the transformed AST, including:
    - multiple targets in one import statement;
    - aliases;
    - selection of profiling targets;
    - preservation of profiling-call order;
    - passing the actual imported objects to the profiler.

    See also:
        Issue #433
    """
    from xml.etree.ElementTree import Element, dump, XMLParser

    input_module = ub.codeblock("""
        import os, sys as system
        from xml.etree.ElementTree import (  # `xml_dump` not profiled
            Element, dump as xml_dump, XMLParser as Parser,
        )
    """)
    mock_prof = _RecordingProfiler()
    with tempfile.TemporaryDirectory() as tmp:
        fpath = ub.Path(tmp) / 'script.py'
        fpath.write_text(input_module)
        module_ast = AstTreeProfiler(
            str(fpath),
            [
                'os',
                'sys',
                'xml.etree.ElementTree.Element',
                'xml.etree.ElementTree.XMLParser',
            ],
            False,
        ).profile()
        namespace = {'profile': mock_prof}
        code = compile(module_ast, str(fpath), 'exec')
        exec(code, namespace)

    assert mock_prof.profiled_objects == [
        os,
        sys,
        Element,
        XMLParser,
    ]
    # Also verify that the aliases created by the original imports resolve
    # to the same objects that were passed to the profiler.
    assert namespace['system'] is sys
    assert namespace['Element'] is Element
    assert namespace['xml_dump'] is dump
    assert namespace['Parser'] is XMLParser


_ImportDiscoveryOption = Literal[
    'conditionals', 'try_except', 'contexts', 'loops',
    'func_defs', 'class_defs',
]
_CompoundStatement = Literal[
    'function-def',
    'async-function-def',  # 3.5+
    'class-def',
    'for-else',
    'async-for-else',  # 3.5+
    'while-else',
    'if-elif-else',
    'match-case',  # 3.10+
    'with',
    'async-with',  # 3.5+
    'try-except-else-finally',
    'try-except*-else-finally',  # 3.11+
]


def _get_toml_import_discovery_section(
    options: set[_ImportDiscoveryOption] | None = None,
) -> str:
    all_options = set(get_args(_ImportDiscoveryOption))
    if options is None:
        options = all_options
    config_file_lines = ['[tool.line_profiler.autoprofile.import_discovery]']
    for option in all_options:
        line = f'{option} = {str(option in options).lower()}'
        config_file_lines.append(line)
    return '\n'.join(config_file_lines)


def _grep_profiled_names(module_text: str) -> list[str]:
    prof_pattern = (
        r'\badd_imported_function_or_module\((\w+(?:\.\w+)*)\)'
    )
    return re.findall(prof_pattern, module_text)


def _propose_module_names(
    prefix: str = 'my_module',
) -> Generator[str, None, None]:
    while True:
        random = str(uuid.uuid4()).replace('-', '_')
        name = f'{prefix}_{random}'
        if not name.isidentifier():
            continue
        if name not in sys.modules:
            yield name


@pytest.mark.parametrize(
    ('prof_mod', 'expected_targets', 'profile_imports', 'profile_whole_file',
     'expect_warnings'),
    [([], [], False, False, False),  # No-op case
     # Whole-file rewriting, with and without import rewriting
     ([], ['bar', 'baz'], True, True, True),
     ([], [], False, True, False),
     # No whole-file rewriting, bu we explicitly ask to profile the
     # `spam.ham.*` import (which can't be done)
     (['spam.ham'], [], False, False, True)])
def test_drop_and_warn_against_star_imports(
    prof_mod: list[str],
    expected_targets: Collection[Literal['bar', 'baz']],
    profile_imports: bool,
    profile_whole_file: bool,
    expect_warnings: bool,
) -> None:
    """
    Test the default behavior of :py:meth:`AstTreeProfiler.profile`:
    that star-imports (``from ... import *``) don't cause it to choke,
    instead just issuing warnings about ignoring them.
    """
    code = ub.codeblock(
        """
        from foo import bar
        from spam.ham import *
        from foobar import baz


        def func() -> None:
            pass
        """,
    ).strip('\n')

    targets_warning_pattern = 'profiling target.* dropped.*'
    warning_checks = [(expect_warnings, targets_warning_pattern, UserWarning)]

    re_checks: list[tuple[str, bool]] = []
    re_checks.append((r'@profile\ndef func', profile_whole_file))
    for target in 'bar', 'baz':
        pattern = rf'add_imported_function_or_module\({target}\)'
        re_checks.append((pattern, target in expected_targets))

    with contextlib.ExitStack() as stack:
        tmpdir = stack.enter_context(tempfile.TemporaryDirectory())
        fpath = ub.Path(tmpdir) / 'script.py'
        fpath.write_text(code)
        if profile_whole_file:
            prof_mod = [*prof_mod, str(fpath)]

        warnings = stack.enter_context(catch_warnings(record=True))
        rewriter = AstTreeProfiler(str(fpath), prof_mod, profile_imports)
        module_ast = rewriter.profile()

    # Check that we no longer get a `SyntaxError` from
    # `add_imported_function_or_module(*)`
    compile(module_ast, str(fpath), 'exec')

    # Check the issuance of warnings related to star-imports
    _check_warnings(warnings, warning_checks)

    # Check the profiling of other targets
    output_module = ast.unparse(module_ast)
    for pattern, expected in re_checks:
        assert bool(re.search(pattern, output_module)) == expected


@pytest.mark.parametrize(
    ('prof_mod', 'expected', 'options'),
    [(['qux', 'quux'], {'qux.jam', 'spam', 'ham', 'eggs'}, {'conditionals'}),
     (['os', 'qux'], {'qux.jam'}, {'conditionals'}),
     (['os', 'qux'], {'fork', 'register_at_fork'}, {'try_except'}),
     (['foobar'], {'baz'}, {'try_except', 'contexts'}),
     (['ersatz_foobar.my_baz'], {'baz'}, {'try_except', 'contexts'}),
     (['os.fork', 'foobar.bar'], {'fork'}, {'try_except', 'contexts'}),
     (['backup_fred', 'operator'], {'methodcaller', 'setitem'},
      {'func_defs'}),
     (['backup_fred', 'operator'], {'__getattr__'},
      {'class_defs'}),
     (['backup_fred', 'operator'], {'fred'}, {'loops'})])
def test_nested_import_discovery(
    prof_mod: list[str],
    expected: set[str],
    options: set[_ImportDiscoveryOption],
) -> None:
    """
    Check the source code transformed by :py:class:`.AstTreeProfiler` to
    see if the import-discovery selection options in the TOML file
    (``[tool.line_profiler.autoprofile.import_discovery]``) are handled
    correctly in a real-ish script, with some of the compound statements
    hosting the import statements nested inside other coumpound
    statements.
    """
    test_module = ub.codeblock("""
    from collections.abc import Generator, Iterable, Mapping
    from contextlib import contextmanager
    from functools import partial
    from importlib import import_module
    from sys import path, version_info
    from typing import Any

    notify_fork = partial(print, 'Forking...')
    try:
        from os import fork
    except Exception:  # Windows
        pass
    else:
        from os import register_at_fork

        register_at_fork(before=notify_fork)

    import foo, bar

    if version_info > (3, 14):
        import qux.jam
        from quux import spam, ham, eggs
    else:
        qux = ham = spam = eggs = None


    @contextmanager
    def _restore_sys_path() -> Generator[None, None, None]:
        from operator import methodcaller, setitem

        old = methodcaller('copy')(path)
        try:
            yield
        finally:
            setitem(path, slice(None), old)


    class MyMapping(Mapping[str, Any]):
        from operator import getitem as __getattr__

        def __getitem__(self, key: str) -> Any:
            ...

        def __iter__(self) -> Iterable[str]:
            ...

        def __len__(self) -> int:
            ...


    with _restore_sys_path():
        try:
            from foobar import baz
        except ImportError:
            from ersatz_foobar import my_baz as baz

    for _fred in 'fred', 'some_fred', 'other_fred':
        try:
            fred = import_module(_fred)
        except ImportError:
            continue
        else:
            del _fred
            break
    else:  # Fallback
        import backup_fred as fred
    """).strip('\n')

    with tempfile.TemporaryDirectory() as tmp:
        mod_fname = os.path.join(tmp, 'test_module.py')
        with open(mod_fname, 'w') as fobj:
            print(test_module, file=fobj)

        cfg_fname = os.path.join(tmp, 'config.toml')
        with open(cfg_fname, 'w') as fobj:
            print(_get_toml_import_discovery_section(options), file=fobj)

        config = ConfigSource.from_config(cfg_fname)
        atp = AstTreeProfiler(mod_fname, prof_mod, False, config=config)
        output_module = ast.unparse(atp.profile())

    for label, module_text in [
        ('input', test_module), ('output', output_module),
    ]:
        print(f'{label.capitalize()}:\n{textwrap.indent(module_text, "  ")}\n')

    assert set(_grep_profiled_names(output_module)) == expected


@pytest.mark.parametrize('inject_options_with', ['config', 'args'])
@pytest.mark.parametrize('use_component',
                         ['ast_tree_profiler', 'ast_profile_transformer'])
@pytest.mark.parametrize(
    ('compound_statement', 'options', 'should_be_profiled'),
    [('function-def', set(), False),
     ('function-def', {'func_defs'}, True),
     ('async-function-def', set(), False),
     ('async-function-def', {'func_defs'}, True),
     ('class-def', set(), False),
     ('class-def', {'class_defs'}, True),
     ('for-else', set(), False),
     ('for-else', {'loops'}, True),
     ('async-for-else', {'func_defs'}, False),
     ('async-for-else', {'func_defs', 'loops'}, True),
     ('while-else', set(), False),
     ('while-else', {'loops'}, True),
     ('if-elif-else', set(), False),
     ('if-elif-else', {'conditionals'}, True),
     ('match-case', set(), False),
     ('match-case', {'conditionals'}, True),
     ('with', set(), False),
     ('with', {'contexts'}, True),
     ('async-with', {'func_defs'}, False),
     ('async-with', {'func_defs', 'contexts'}, True),
     ('try-except-else-finally', set(), False),
     ('try-except-else-finally', {'try_except'}, True),
     ('try-except*-else-finally', set(), False),
     ('try-except*-else-finally', {'try_except'}, True)])
def test_import_discovery_in_all_compound_statements(
    compound_statement: _CompoundStatement,
    options: set[_ImportDiscoveryOption],
    use_component: Literal['ast_tree_profiler', 'ast_profile_transformer'],
    inject_options_with: Literal['config', 'args'],
    should_be_profiled: bool,
) -> None:
    """
    Exhaustive "unit" test for imports nested in all the kwown
    compound-statement language constructions, and all their respective
    config-level switches.

    Notes:
        - If a construction is not valid in the current Python version,
          the subtest is skipped.

        - Some ``async`` constructions are nested inside a coroutine
          definition by necessity.
    """
    test_cases = {
        'function-def': """
        def func():
            import foo, bar
            import baz
            import foobar

            ...
        """,
        'async-function-def': """
        async def coroutine(awaitable):
            import foo
            import bar
            import baz, foobar

            await awaitable
        """,
        'class-def': """
        class Class:
            import foo
            import bar, baz
            import foobar

            ...
        """,
        'for-else': """
        for _ in range(5):
            import foo
            import bar

            ...
        else:
            import baz
            import foobar

            ...
        """,
        'async-for-else': """
        async def agen(awaitable):
            async for x in (await awaitable):
                import foo, bar

                yield x
            else:
                import baz, foobar
                ...
        """,
        'while-else': """
        while True:
            import foo, bar, baz
            ...
        else:
            import foobar
        """,
        'if-elif-else': """
        if True:
            import foo
        elif False:
            import bar, baz
        else:
            import foobar
        """,
        'match-case': """
        match [1, 2, 3]:
            case [1, *a, 2]:
                import foo
                ...
            case [1, 2, 3, b]:
                import bar
                ...
            case [1, *c]:
                import baz
                ...
            case _:
                import foobar
        """,
        'with': """
        with ctx:
            import foo, bar, baz, foobar
            ...
        """,
        'async-with': """
        async def afunc():
            async with actx:
                import foo
                import bar, baz, foobar
                ...
        """,
        'try-except-else-finally': """
        try:
            import foo
        except ImportError:
            import bar
        else:
            import baz
        finally:
            import foobar
        """,
    }
    test_cases['try-except*-else-finally'] = (
        test_cases['try-except-else-finally'].replace('except', 'except*')
    )
    version_bounds = {
        'async-function-def': (3, 5),
        'async-def': (3, 5),
        'async-for-else': (3, 5),
        'async-with': (3, 5),
        'match-case': (3, 10),
        'try-except*-else-finally': (3, 11),
    }
    all_names = {'foo', 'bar', 'baz', 'foobar'}

    test_case = ub.codeblock(test_cases[compound_statement]).strip('\n')
    version_bound: tuple[int, ...] = version_bounds.get(compound_statement, ())
    if sys.version_info < version_bound:
        version = '.'.join(str(v) for v in sys.version_info[:3])
        pytest.skip(reason=f'cannot test {compound_statement} on {version}')

    with tempfile.TemporaryDirectory() as tmp:
        case_fname = os.path.join(tmp, 'test_case.py')
        with open(case_fname, 'w') as fobj:
            print(test_case, file=fobj)

        if inject_options_with == 'config':
            cfg_fname = os.path.join(tmp, 'config.toml')
            with open(cfg_fname, 'w') as fobj:
                print(_get_toml_import_discovery_section(options), file=fobj)

            config: ConfigSource | None
            profile_nested_imports: Collection[_ImportDiscoveryOption] | None

            config = ConfigSource.from_config(cfg_fname)
            profile_nested_imports = None
        else:  # Explicitly passed via args
            config, profile_nested_imports = None, options

        if use_component == 'ast_tree_profiler':
            atp = AstTreeProfiler(
                # `profile_imports=False` prevents
                # `AstProfileTransformer` from rewriting the imports, so
                # we're really testing `ProfmodExtractor` here
                case_fname, list(all_names), False,
                config=config,
            )
            module_ast = atp.profile(
                profile_nested_imports=profile_nested_imports,
            )
        else:
            module_ast = AstProfileTransformer._transform(
                ast.parse(test_case),
                case_fname,
                profile_imports=True,
                config=config,
                profile_nested_imports=profile_nested_imports,
            )
        output = ast.unparse(module_ast)

    for label, module_text in [
        ('input', test_case), ('output', output),
    ]:
        print(f'{label.capitalize()}:\n{textwrap.indent(module_text, "  ")}\n')

    expected = all_names if should_be_profiled else set()
    assert set(_grep_profiled_names(output)) == expected


@pytest.mark.parametrize(
    ('call', 'use_component', 'expected_profiled_objects'),
    [
        # Nothing special happens when calling `first()` and `second()`,
        # there's only a single import target (`textwrap.indent`) inside
        # the function
        ('first', 'profmod_extractor', ['indent']),
        ('first', 'ast_tree_profiler', ['indent']),
        ('first', 'ast_profile_transformer', ['indent']),
        ('second', 'profmod_extractor', ['indent']),
        ('second', 'ast_tree_profiler', ['indent']),
        ('second', 'ast_profile_transformer', ['indent']),
        # With `third()`, because `textwrap.dedent()` is also imported:
        ('third', 'profmod_extractor', ['indent']),
        # - When using `AstTreeProfiler`, `ProfmodExtractor` first
        #   inserts a profiling node for `indent()`, then followed by
        #   another for `dedent()` inserted by `AstProfileTransformer`;
        #   since the profiling node for `dedent()` is created later, it
        #   is inserted bewteen the import statement and the profiling
        #   node for `indent()`, and is hence executed first
        ('third', 'ast_tree_profiler', ['dedent', 'indent']),
        # - When using `AstProfileTransformer`, profiling nodes are
        #   inserted for both `indent()` and `dedent()` in one go
        ('third', 'ast_profile_transformer', ['indent', 'dedent']),
    ])
def test_nested_imports_correct_deduplication_across_scopes(
    call: Literal['first', 'second', 'third'],
    use_component: Literal[
        'profmod_extractor', 'ast_tree_profiler', 'ast_profile_transformer',
    ],
    expected_profiled_objects: Sequence[Literal['indent', 'dedent']],
) -> None:
    """
    Test that there is no aliasing in the check we have against
    inserting duplicate ``profile.add_imported_function_or_module(...)``
    statements: duplicates should only be counted within the same scope.

    Note:
        - At runtime, duplicates don't really matter in terms of
          CORRECTNESS, because ultimately
          :py:class:`line_profiler.LineProfiler.add_callable` is
          idempotent.

        - However, since calls to
          :py:func:`line_profiler.autoprofile.line_profiler_utils\
.add_imported_function_or_module`
          can result in arbitrary deep descent into the profiled object,
          these interpolated calls can have an impact on the
          PERFORMANCE, especially when inserted into function/method
          bodies. For this reason, import discovery in function bodies
          is off by default.
    """
    test_module = ub.codeblock("""
    def first() -> str:
        from textwrap import indent

        return indent('first', '  ')


    def second() -> str:
        from textwrap import indent as ind

        return ind('second', '  ')


    def third() -> str:
        from textwrap import indent, dedent
        from textwrap import indent as _indent  # Duplicate

        return _indent('third', '  ')
    """).strip('\n')

    mock_prof = _RecordingProfiler()
    with tempfile.TemporaryDirectory() as tmp:
        mod_fname = os.path.join(tmp, 'test_module.py')
        with open(mod_fname, 'w') as fobj:
            print(test_module, file=fobj)

        cfg_fname = os.path.join(tmp, 'config.toml')
        with open(cfg_fname, 'w') as fobj:
            print(_get_toml_import_discovery_section(), file=fobj)

        config = ConfigSource.from_config(cfg_fname)
        if use_component == 'profmod_extractor':
            # Ditto comment in
            # `test_import_discovery_in_all_compound_statements()`
            mod_ast = AstTreeProfiler(
                mod_fname, ['textwrap.indent'], False,
                config=config,
            ).profile()
        elif use_component == 'ast_tree_profiler':
            # Integration of both
            mod_ast = AstTreeProfiler(
                mod_fname, ['textwrap.indent', str(mod_fname)], True,
                config=config,
            ).profile()
        else:  # `ast_profile_transformer`
            mod_ast = AstProfileTransformer._transform(
                ast.parse(test_module),
                mod_fname,
                profile_imports=True,
                config=config,
            )
            # We need this to actually compile and exec the code
            mod_ast = ast.fix_missing_locations(mod_ast)
        print(ast.unparse(mod_ast))

        namespace: dict[str, Any] = {'profile': mock_prof}
        code = compile(mod_ast, mod_fname, 'exec')
        exec(code, namespace)

    # Make the call; regardless of which of the functions is called,
    # `textwrap.indent()` should be presented to the profiler exactly
    # once
    assert namespace[call]() == '  ' + call
    profiled_objects = [func.__name__ for func in mock_prof.profiled_objects]
    assert profiled_objects == list(expected_profiled_objects)


@pytest.mark.parametrize(
    ('definitions', 'expected'),
    [
        # The profiling statement is always inserted into the first
        # function body where the import occurs...
        (['foo'], {'indent'}), (['bar'], {'ind'}),
        # ... but only the first
        (['foo', 'bar'], {'indent'}), (['bar', 'foo'], {'ind'}),
    ])
def test_ast_profile_transformer_deprecated_profiled_imports(
    definitions: Sequence[Literal['foo', 'bar']],
    expected: Collection[Literal['indent', 'ind']],
) -> None:
    """
    Test that the legacy invocation of
    :py:class:`.AstProfileTransformer` with
    ``profiled_imports: Collection[str]`` works "as expected":

    - :py:class:`DeprecationWarning` is issued, instructing users to
      switch to the mapping form of the argument.

    - Deduplication of imports happens without regard of scopes.

    See also:
        :py:func:\
`test_nested_imports_correct_deduplication_across_scopes`
    """
    defs = {
        'foo': """
        def foo() -> str:
            from textwrap import indent

            return indent('foo', '  ')
        """,
        'bar': """
        def bar() -> str:
            from textwrap import indent as ind

            return ind('bar', '  ')
        """,
    }
    test_module = '\n\n'.join(
        ub.codeblock(defs[func]).strip('\n') for func in definitions
    )

    with contextlib.ExitStack() as stack:
        tmp = stack.enter_context(tempfile.TemporaryDirectory())

        mod_fname = os.path.join(tmp, 'test_module.py')
        with open(mod_fname, 'w') as fobj:
            print(test_module, file=fobj)

        cfg_fname = os.path.join(tmp, 'config.toml')
        with open(cfg_fname, 'w') as fobj:
            print(_get_toml_import_discovery_section(), file=fobj)

        stack.enter_context(pytest.warns(
            DeprecationWarning,
            match='.*'.join(
                '{}{}{}'.format(
                    r'\b' if chunk[0].isalnum() else '',
                    chunk,
                    r'\b' if chunk[-1].isalnum() else '',
                )
                for chunk in [
                    'profiled_imports=', r'Collection\[str\]', 'deprecated',
                    'use', r'Mapping\[.+, .+\]', 'or', 'None',
                ]
            )
        ))

        config = ConfigSource.from_config(cfg_fname)
        mod_ast = AstProfileTransformer._transform(
            ast.parse(test_module),
            mod_fname,
            profile_imports=True,
            profiled_imports=[],  # This triggers legacy behavior
            config=config,
        )
        output = ast.unparse(mod_ast)
        print(output)

    assert set(_grep_profiled_names(output)) == set(expected)


@pytest.mark.parametrize(
    ('targets', 'dunder_all', 'expected_imports', 'expected_profiled'),
    [
        # No `__all__` -> all the public names included
        (None, None, {'indent', 'foo', 'bar'}, {'indent', 'foo', 'bar'}),
        # `_baz` specified as a target, but is never imported to begin
        # with
        ({'__module__.bar', '__module__._baz'}, None,
         {'indent', 'foo', 'bar'}, {'bar'}),
        # With a valid `__all__`, only names inside will be imported
        (None, ['foo', '_baz', '_dedent'],
         {'foo', '_dedent', '_baz'}, {'foo', 'dedent', '_baz'}),
        ({'__module__._baz', '__module__._dedent'}, ['foo', '_baz', '_dedent'],
         {'foo', '_dedent', '_baz'}, {'dedent', '_baz'}),
    ])
def test_add_star_import(
    targets: Collection[str] | None,
    dunder_all: Sequence[str] | None,
    expected_imports: Collection[str],
    expected_profiled: Collection[str],
) -> None:
    """
    Test that :py:func:`.add_star_import` works as expected, retriving
    the correct names from the namespace and profiling them.
    """
    test_module = ub.codeblock("""
    from textwrap import indent, dedent as _dedent


    def foo() -> None:
        ...


    def bar() -> None:
        ...


    def _baz() -> None:
        ...
    """).strip('\n')
    if dunder_all is not None:
        all_repr = repr(dunder_all)
        assert ast.literal_eval(all_repr) == dunder_all
        test_module = f'{test_module}\n\n__all__ = {all_repr}'

    module_name = next(_propose_module_names())
    if targets is not None:
        targets = [
            t.replace('__module__', module_name) for t in targets
        ]
    mock_prof = _RecordingProfiler()
    with contextlib.ExitStack() as stack:
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        mp = stack.enter_context(pytest.MonkeyPatch.context())
        mp.syspath_prepend(tmp)

        mod_fname = os.path.join(tmp, module_name + '.py')
        with open(mod_fname, 'w') as fobj:
            print(test_module, file=fobj)

        # Check that the correct names are imported by the star-import
        namespace: dict[str, Any] = {'baz': None, '__builtins__': builtins}
        preexisting = set(namespace)
        exec(f'from {module_name} import *', namespace)
        assert set(namespace) == set(expected_imports) | preexisting

        # Check that the same names are passed to the profiler by
        # `add_star_import()`
        add_star_import(mock_prof, module_name, targets, namespace)
        profiled_objects = {
            func.__name__ for func in mock_prof.profiled_objects
        }
        assert profiled_objects == set(expected_profiled)
