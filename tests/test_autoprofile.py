import tempfile
import pathlib
import shutil
import sys
import os
import subprocess
import textwrap
from subprocess import PIPE


def test_single_function_autoprofile():
    """
    Test that every function in a file is profiled when autoprofile is enabled.
    """
    temp_dpath = pathlib.Path(tempfile.mkdtemp())

    code = textwrap.dedent(
        '''
        def func1(a):
            return a + 1

        func1(1)
        ''').strip()
    with ChDir(temp_dpath):

        script_fpath = pathlib.Path('script.py')
        script_fpath.write_text(code)

        args = [sys.executable, '-m', 'kernprof', '-p', 'script.py', '-l', os.fspath(script_fpath)]
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              universal_newlines=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [sys.executable, '-m', 'line_profiler', os.fspath(script_fpath) + '.lprof']
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              universal_newlines=True)
        raw_output = proc.stdout
        proc.check_returncode()

    assert 'func1' in raw_output
    shutil.rmtree(temp_dpath)


def test_multi_function_autoprofile():
    """
    Test that every function in a file is profiled when autoprofile is enabled.
    """
    temp_dpath = pathlib.Path(tempfile.mkdtemp())

    code = textwrap.dedent(
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
        ''').strip()
    with ChDir(temp_dpath):

        script_fpath = pathlib.Path('script.py')
        script_fpath.write_text(code)

        args = [sys.executable, '-m', 'kernprof', '-p', 'script.py', '-l', os.fspath(script_fpath)]
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              universal_newlines=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [sys.executable, '-m', 'line_profiler', os.fspath(script_fpath) + '.lprof']
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              universal_newlines=True)
        raw_output = proc.stdout
        proc.check_returncode()

    assert 'func1' in raw_output
    assert 'func2' in raw_output
    assert 'func3' in raw_output
    assert 'func4' in raw_output

    shutil.rmtree(temp_dpath)


class ChDir:
    """
    Context manager that changes the current working directory and then
    returns you to where you were.

    This is nearly the same as the stdlib :func:`contextlib.chdir`, with the
    exception that it will do nothing if the input path is None (i.e. the user
    did not want to change directories).

    Args:
        dpath (str | PathLike | None):
            The new directory to work in.
            If None, then the context manager is disabled.

    SeeAlso:
        :func:`contextlib.chdir`
    """
    def __init__(self, dpath):
        self._context_dpath = dpath
        self._orig_dpath = None

    def __enter__(self):
        """
        Returns:
            ChDir: self
        """
        if self._context_dpath is not None:
            self._orig_dpath = os.getcwd()
            os.chdir(self._context_dpath)
        return self

    def __exit__(self, ex_type, ex_value, ex_traceback):
        """
        Args:
            ex_type (Type[BaseException] | None):
            ex_value (BaseException | None):
            ex_traceback (TracebackType | None):

        Returns:
            bool | None
        """
        if self._context_dpath is not None:
            os.chdir(self._orig_dpath)
