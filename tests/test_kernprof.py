import contextlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

import pytest
import ubelt as ub

from kernprof import main, ContextualProfile


def f(x):
    """ A function. """
    y = x + 10
    return y


def g(x):
    """ A generator. """
    y = yield x + 10
    yield y + 20


@pytest.mark.parametrize(
    'use_kernprof_exec, args, expected_output, expect_error',
    [([False, ['-m'], '', True]),
     # `python -m kernprof`
     (False, ['-m', 'mymod'], "[__MYMOD__]", False),
     # `kernprof`
     (True, ['-m', 'mymod'], "[__MYMOD__]", False),
     (False, ['-m', 'mymod', '-p', 'bar'], "[__MYMOD__, '-p', 'bar']", False),
     # `-p bar` consumed by `kernprof`, `-p baz` are not
     (False,
      ['-p', 'bar', '-m', 'mymod', '-p', 'baz'],
      "[__MYMOD__, '-p', 'baz']",
      False),
     # Separator `--` broke off the remainder, so the requisite arg for
     # `-m` is not found and we error out
     (False, ['-p', 'bar', '-m', '--', 'mymod', '-p', 'baz'], '', True),
     # Separator `--` broke off the remainder, so `-m` is passed to the
     # script instead of being parsed as the module to execute
     (False,
      ['-p', 'bar', 'mymod.py', '--', '-m', 'mymod', '-p', 'baz'],
      "['mymod.py', '-m', 'mymod', '-p', 'baz']",
      False)])
def test_kernprof_m_parsing(
        use_kernprof_exec, args, expected_output, expect_error):
    """
    Test that `kernprof -m` behaves like `python -m` in that it requires
    an argument and cuts off everything after it, passing that along
    to the module to be executed.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dpath = ub.Path(tmpdir)
        mod = (temp_dpath / 'mymod.py').resolve()
        mod.write_text(ub.codeblock(
            '''
            import sys


            if __name__ == '__main__':
                print(sys.argv)
            '''))
        if use_kernprof_exec:
            cmd = ['kernprof']
        else:
            cmd = [sys.executable, '-m', 'kernprof']
        proc = ub.cmd(cmd + args, cwd=temp_dpath, verbose=2)
    if expect_error:
        assert proc.returncode
        return
    else:
        proc.check_returncode()
    expected_output = re.escape(expected_output).replace(
        '__MYMOD__', "'.*{}'".format(re.escape(os.path.sep + mod.name)))
    assert re.match('^' + expected_output, proc.stdout)


@pytest.mark.skipif(sys.version_info[:2] < (3, 11),
                    reason='no `@enum.global_enum` in Python '
                    f'{".".join(str(v) for v in sys.version_info[:3])}')
@pytest.mark.parametrize(('flags', 'profiled_main'),
                         [('-lv -p mymod', True),  # w/autoprofile
                          ('-lv', True),  # w/o autoprofile
                          ('-b', False)])  # w/o line profiling
def test_kernprof_m_sys_modules(flags, profiled_main):
    """
    Test that `kernprof -m` is amenable to modules relying on the global
    `sys` state (e.g. those using `@enum.global_enum`).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dpath = ub.Path(tmpdir)
        (temp_dpath / 'mymod.py').write_text(ub.codeblock(
            '''
            import enum
            import os
            import sys


            @enum.global_enum
            class MyEnum(enum.Enum):
                FOO = 1
                BAR = 2


            @profile
            def main():
                x = FOO.value + BAR.value
                print(x)
                assert x == 3


            if __name__ == '__main__':
                main()
            '''))
        cmd = [sys.executable, '-m', 'kernprof',
               *shlex.split(flags), '-m', 'mymod']
        proc = ub.cmd(cmd, cwd=temp_dpath, verbose=2)
    proc.check_returncode()
    assert proc.stdout.startswith('3\n')
    assert ('Function: main' in proc.stdout) == profiled_main


@pytest.mark.parametrize('autoprof', [True, False])
@pytest.mark.parametrize('static', [True, False])
def test_kernprof_m_import_resolution(static, autoprof):
    """
    Test that `kernprof -m` resolves the module using static and dynamic
    as is appropriate (toggled by the undocumented environment variable
    :env:`LINE_PROFILER_STATIC_ANALYSIS`; note that static analysis
    doesn't handle namespace modules while dynamic resolution does.
    """
    code = ub.codeblock('''
    import enum
    import os
    import sys


    @profile
    def main():
        print('Hello world')


    if __name__ == '__main__':
        main()
    ''')
    cmd = [sys.executable, '-m', 'kernprof', '-lv']
    if autoprof:
        # Remove the explicit decorator, and use the `--prof-mod` flag
        code = '\n'.join(line for line in code.splitlines()
                         if '@profile' not in line)
        cmd += ['-p', 'my_namesapce_pkg.mysubmod']
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dpath = ub.Path(tmpdir)
        namespace_mod_path = temp_dpath / 'my_namesapce_pkg' / 'mysubmod.py'
        namespace_mod_path.parent.mkdir()
        namespace_mod_path.write_text(code)
        python_path = tmpdir
        if 'PYTHONPATH' in os.environ:
            python_path += ':' + os.environ['PYTHONPATH']
        env = {**os.environ,
               # Toggle use of static analysis
               'LINE_PROFILER_STATIC_ANALYSIS': str(bool(static)),
               # Add the tempdir to `sys.path`
               'PYTHONPATH': python_path}
        cmd += ['-m', 'my_namesapce_pkg.mysubmod']
        proc = ub.cmd(cmd, cwd=temp_dpath, verbose=2, env=env)
    if static:
        assert proc.returncode
        assert proc.stderr.startswith('Could not find module')
    else:
        proc.check_returncode()
        assert proc.stdout.startswith('Hello world\n')
        assert 'Function: main' in proc.stdout


@pytest.mark.parametrize('error', [True, False])
@pytest.mark.parametrize(
    'args',
    ['', '-pmymod'],  # Normal execution / auto-profile
)
def test_kernprof_sys_restoration(capsys, error, args):
    """
    Test that `kernprof.main()` and
    `line_profiler.autoprofile.autoprofile.run()` (resp.) properly
    restores `sys.path` (resp. `sys.modules['__main__']`) on the way
    out.

    Notes
    -----
    The test is run in-process.
    """
    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        tmpdir = enter(tempfile.TemporaryDirectory())
        assert tmpdir not in sys.path
        temp_dpath = ub.Path(tmpdir)
        (temp_dpath / 'mymod.py').write_text(ub.codeblock(
            f'''
            import sys


            def main():
                # Mess up `sys.path`
                sys.path.append({tmpdir!r})
                # Output
                print(1)
                # Optionally raise an error
                if {error!r}:
                    raise Exception


            if __name__ == '__main__':
                main()
            '''))
        enter(ub.ChDir(tmpdir))
        if error:
            ctx = pytest.raises(BaseException)
        else:
            ctx = contextlib.nullcontext()
        old_modules = sys.modules.copy()
        try:
            old_main = sys.modules.get('__main__')
            with ctx:
                main(['-l', *shlex.split(args), '-m', 'mymod'])
            out, _ = capsys.readouterr()
            assert out.startswith('1')
            assert tmpdir not in sys.path
            assert sys.modules.get('__main__') is old_main
        finally:
            sys.modules.clear()
            sys.modules.update(old_modules)


@pytest.mark.parametrize(
    ('flags', 'expected_stdout', 'expected_stderr'),
    [('',  # Neutral verbosity level
      {'^Output to stdout': True,
       r"^Wrote .* '.*script\.py\.lprof'": True,
       r'^Inspect results with:''\n'
       r'python -m line_profiler .*script\.py\.lprof': True,
       r'line_profiler\.autoprofile\.autoprofile'
       r'\.run\(\n(?:.+,\n)*.*\)': False,
       r'^\[kernprof .*\]': False,
       r'^Function: main': False},
      {'^Output to stderr': True}),
     ('--view',  # Verbosity level 1 = `--view`
      {'^Output to stdout': True,
       r"^Wrote .* '.*script\.py\.lprof'": True,
       r'^Inspect results with:''\n'
       r'python -m line_profiler .*script\.py\.lprof': False,
       r'line_profiler\.autoprofile\.autoprofile'
       r'\.run\(\n(?:.+,\n)*.*\)': False,
       r'^\[kernprof .*\]': False,
       r'^Function: main': True},
      {'^Output to stderr': True}),
     ('-vv',  # Verbosity level 2, show diagnostics
      {'^Output to stdout': True,
       r"^\[kernprof .*\] Wrote .* '.*script\.py\.lprof'": True,
       r'Inspect results with:''\n'
       r'python -m line_profiler .*script\.py\.lprof': False,
       r'line_profiler\.autoprofile\.autoprofile'
       r'\.run\(\n(?:.+,\n)*.*\)': True,
       r'^Function: main': True},
      {'^Output to stderr': True}),
     # Verbosity level -1, suppress `kernprof` output
     ('--quiet',
      {'^Output to stdout': True, 'Wrote': False},
      {'^Output to stderr': True}),
     # Verbosity level -2, suppress script stdout
     # (also test verbosity arithmatics)
     ('--quiet --quiet --verbose -q', None, {'^Output to stderr': True}),
     # Verbosity level -3, suppress script stderr
     ('-qq --quiet', None,
      # This should have been `None`, but there's something weird with
      # `coverage` in CI which causes a spurious warning...
      {'^Output to stderr': False})])
def test_kernprof_verbosity(flags, expected_stdout, expected_stderr):
    """
    Test the various verbosity levels of `kernprof`.
    """
    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        tmpdir = enter(tempfile.TemporaryDirectory())
        temp_dpath = ub.Path(tmpdir)
        (temp_dpath / 'script.py').write_text(ub.codeblock(
            '''
            import sys


            def main():
                print('Output to stdout', file=sys.stdout)
                print('Output to stderr', file=sys.stderr)


            if __name__ == '__main__':
                main()
            '''))
        enter(ub.ChDir(tmpdir))
        proc = ub.cmd(['kernprof', '-l',
                       # Add an eager pre-import target
                       '-p', 'script.py', '-p', 'zipfile', '-z',
                       *shlex.split(flags), 'script.py'])
    proc.check_returncode()
    print(proc.stdout)
    for expected_outputs, stream in [(expected_stdout, proc.stdout),
                                     (expected_stderr, proc.stderr)]:
        if expected_outputs is None:
            assert not stream
            continue
        for pattern, expect_match in expected_outputs.items():
            found = re.search(pattern, stream, flags=re.MULTILINE)
            if not bool(found) == expect_match:
                msg = ub.paragraph(
                    f'''
                    Searching for pattern: {pattern!r} in output.
                    Did we expect a match? {expect_match!r}.
                    Did we get a match? {bool(found)!r}.
                    ''')
                raise AssertionError(msg)


def test_kernprof_eager_preimport_bad_module():
    """
    Test for the preservation of the full traceback when an error occurs
    in an auto-generated pre-import module.
    """
    bad_module = '''raise Exception('Boo')'''
    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        tmpdir = enter(tempfile.TemporaryDirectory())
        temp_dpath = ub.Path(tmpdir)
        (temp_dpath / 'my_bad_module.py').write_text(bad_module)
        enter(ub.ChDir(tmpdir))
        python_path = os.environ.get('PYTHONPATH', '')
        if python_path:
            python_path = f'{python_path}:{tmpdir}'
        else:
            python_path = tmpdir
        proc = ub.cmd(['kernprof', '-l',
                       # Add an eager pre-import target
                       '-pmy_bad_module', '-c', 'print(1)'],
                      env={**os.environ, 'PYTHONPATH': python_path})
    # Check that the traceback is preserved
    print(proc.stdout)
    print(proc.stderr, file=sys.stderr)
    assert proc.returncode
    assert 'import my_bad_module' in proc.stderr
    assert bad_module in proc.stderr
    # Check that the generated tempfiles are wiped
    reverse_iter_lines = iter(reversed(proc.stderr.splitlines()))
    next(line for line in reverse_iter_lines if 'import my_bad_module' in line)
    tb_header = next(reverse_iter_lines).strip()
    match = re.match('File ([\'"])(.+)\\1, line [0-9]+, in .*', tb_header)
    assert match
    tmp_mod = match.group(2)
    assert not os.path.exists(tmp_mod)
    assert not os.path.exists(os.path.dirname(tmp_mod))


@pytest.mark.parametrize('stdin', [True, False])
def test_kernprof_bad_temp_script(stdin):
    """
    Test for the preservation of the full traceback when an error occurs
    in a temporary script supplied via `kernprof -c` or `kernprof -`.
    """
    bad_script = '''1 / 0'''
    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        enter(ub.ChDir(enter(tempfile.TemporaryDirectory())))
        if stdin:
            proc = subprocess.run(
                ['kernprof', '-'],
                input=bad_script, capture_output=True, text=True)
        else:
            proc = subprocess.run(['kernprof', '-c', bad_script],
                                  capture_output=True, text=True)
    # Check that the traceback is preserved
    print(proc.stdout)
    print(proc.stderr, file=sys.stderr)
    assert proc.returncode
    assert '1 / 0' in proc.stderr
    assert 'ZeroDivisionError' in proc.stderr
    # Check that the generated tempfiles are wiped
    reverse_iter_lines = iter(reversed(proc.stderr.splitlines()))
    next(line for line in reverse_iter_lines if '1 / 0' in line)
    tb_header = next(reverse_iter_lines).strip()
    match = re.match('File ([\'"])(.+)\\1, line [0-9]+, in .*', tb_header)
    assert match
    tmp_script = match.group(2)
    assert not os.path.exists(tmp_script)
    assert not os.path.exists(os.path.dirname(tmp_script))


@pytest.mark.parametrize('debug', [True, False])
def test_bad_prof_mod_target(debug):
    """
    Test the handling of bad paths in `--prof-mod` targets.
    """
    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        enter(ub.ChDir(enter(tempfile.TemporaryDirectory())))
        proc = ub.cmd(['kernprof', '-l', '-p', './nonexistent.py',
                       '-c', 'print("Output: foo")'],
                      env={**os.environ, 'LINE_PROFILER_DEBUG': str(debug)})
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        proc.check_returncode()
        assert os.listdir()  # Profile results
    assert 'Output: foo' in proc.stdout
    assert re.search(r"1 .* target .*: \['\./nonexistent\.py'\]", proc.stderr)


@pytest.mark.parametrize('builtin', [True, False])
@pytest.mark.parametrize('module', [True, False])
def test_call_with_diagnostics(module, builtin):
    """
    Test the output of call signatures in debug messages.
    """
    to_run = ['-m', 'calendar'] if module else ['-c', 'print("Output: foo")']
    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        enter(ub.ChDir(enter(tempfile.TemporaryDirectory())))
        cmd = ['kernprof']
        if builtin:
            cmd += ['-b']
        proc = ub.cmd(cmd + to_run,
                      env={**os.environ, 'LINE_PROFILER_DEBUG': 'true'})
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        proc.check_returncode()
        assert os.listdir()  # Profile results
    has_runctx_call = re.search(
        r'Calling: .+\.runctx\(.+\)', proc.stdout, flags=re.DOTALL)
    has_execfile_call = re.search(
        r'execfile\(.+\)', proc.stdout, flags=re.DOTALL)
    assert bool(has_runctx_call) == (not builtin)
    assert bool(has_execfile_call) == (not module)


class TestKernprof(unittest.TestCase):

    def test_enable_disable(self):
        profile = ContextualProfile()
        self.assertEqual(profile.enable_count, 0)
        profile.enable_by_count()
        self.assertEqual(profile.enable_count, 1)
        profile.enable_by_count()
        self.assertEqual(profile.enable_count, 2)
        profile.disable_by_count()
        self.assertEqual(profile.enable_count, 1)
        profile.disable_by_count()
        self.assertEqual(profile.enable_count, 0)
        profile.disable_by_count()
        self.assertEqual(profile.enable_count, 0)

        with profile:
            self.assertEqual(profile.enable_count, 1)
            with profile:
                self.assertEqual(profile.enable_count, 2)
            self.assertEqual(profile.enable_count, 1)
        self.assertEqual(profile.enable_count, 0)

        with self.assertRaises(RuntimeError):
            self.assertEqual(profile.enable_count, 0)
            with profile:
                self.assertEqual(profile.enable_count, 1)
                raise RuntimeError()
        self.assertEqual(profile.enable_count, 0)

    def test_function_decorator(self):
        profile = ContextualProfile()
        f_wrapped = profile(f)
        self.assertEqual(f_wrapped.__name__, f.__name__)
        self.assertEqual(f_wrapped.__doc__, f.__doc__)

        self.assertEqual(profile.enable_count, 0)
        value = f_wrapped(10)
        self.assertEqual(profile.enable_count, 0)
        self.assertEqual(value, f(10))

    def test_gen_decorator(self):
        profile = ContextualProfile()
        g_wrapped = profile(g)
        self.assertEqual(g_wrapped.__name__, g.__name__)
        self.assertEqual(g_wrapped.__doc__, g.__doc__)

        self.assertEqual(profile.enable_count, 0)
        i = g_wrapped(10)
        self.assertEqual(profile.enable_count, 0)
        self.assertEqual(next(i), 20)
        self.assertEqual(profile.enable_count, 0)
        self.assertEqual(i.send(30), 50)
        self.assertEqual(profile.enable_count, 0)

        with self.assertRaises((StopIteration, RuntimeError)):
            next(i)
        self.assertEqual(profile.enable_count, 0)


if __name__ == '__main__':
    """
    CommandLine:
        python ./tests/test_kernprof.py
    """
    unittest.main()
