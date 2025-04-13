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
     (False, ['-m', 'mymod'], "['mymod']", False),
     # `kernprof`
     (True, ['-m', 'mymod'], "['mymod']", False),
     (False, ['-m', 'mymod', '-p', 'bar'], "['mymod', '-p', 'bar']", False),
     # `-p bar` consumed by `kernprof`, `-p baz` are not
     (False,
      ['-p', 'bar', '-m', 'mymod', '-p', 'baz'],
      "['mymod', '-p', 'baz']",
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
    temp_dpath = ub.Path(tempfile.mkdtemp())
    (temp_dpath / 'mymod.py').write_text(ub.codeblock(
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
    assert proc.stdout.startswith(expected_output)


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
