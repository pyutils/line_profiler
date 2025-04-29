import os
import re
import shlex
import sys
import tempfile
import unittest

import pytest
import ubelt as ub

from kernprof import ContextualProfile


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
