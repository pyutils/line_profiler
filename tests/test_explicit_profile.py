import tempfile
import pathlib
import shutil
import sys
import os
import subprocess
import textwrap
from subprocess import PIPE


def _demo_explicit_profile_script():
    return textwrap.dedent(
        '''
        from line_profiler import profile

        @profile
        def fib(n):
            a, b = 0, 1
            while a < n:
                a, b = b, a + b

        fib(10)
        ''').strip()


def test_explicit_profile_with_nothing():
    """
    Test that no profiling happens when we dont request it.
    """
    temp_dpath = pathlib.Path(tempfile.mkdtemp())
    with ChDir(temp_dpath):

        script_fpath = pathlib.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())

        args = [sys.executable, os.fspath(script_fpath)]
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              universal_newlines=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    assert not (temp_dpath / 'profile_output.txt').exists()
    assert not (temp_dpath / 'profile_output.lprof').exists()
    shutil.rmtree(temp_dpath)


def test_explicit_profile_with_environ_on():
    """
    Test that explicit profiling is enabled when we specify the LINE_PROFILE
    enviornment variable.
    """
    temp_dpath = pathlib.Path(tempfile.mkdtemp())
    env = os.environ.copy()
    env['LINE_PROFILE'] = '1'

    with ChDir(temp_dpath):

        script_fpath = pathlib.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())

        args = [sys.executable, os.fspath(script_fpath)]
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              env=env,
                              universal_newlines=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    assert (temp_dpath / 'profile_output.txt').exists()
    assert (temp_dpath / 'profile_output.lprof').exists()
    shutil.rmtree(temp_dpath)


def test_explicit_profile_with_environ_off():
    """
    When LINE_PROFILE is falsy, profiling should not run.
    """
    temp_dpath = pathlib.Path(tempfile.mkdtemp())
    env = os.environ.copy()
    env['LINE_PROFILE'] = '0'

    with ChDir(temp_dpath):

        script_fpath = pathlib.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())

        args = [sys.executable, os.fspath(script_fpath)]
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              env=env,
                              universal_newlines=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    assert not (temp_dpath / 'profile_output.txt').exists()
    assert not (temp_dpath / 'profile_output.lprof').exists()
    shutil.rmtree(temp_dpath)


def test_explicit_profile_with_cmdline():
    """
    Test that explicit profiling is enabled when we specify the --line-profile
    command line flag.

    xdoctest ~/code/line_profiler/tests/test_explicit_profile.py test_explicit_profile_with_environ
    """
    temp_dpath = pathlib.Path(tempfile.mkdtemp())

    with ChDir(temp_dpath):

        script_fpath = pathlib.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())

        args = [sys.executable, os.fspath(script_fpath), '--line-profile']
        print(f'args={args}')
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              universal_newlines=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    assert (temp_dpath / 'profile_output.txt').exists()
    assert (temp_dpath / 'profile_output.lprof').exists()
    shutil.rmtree(temp_dpath)


def test_explicit_profile_with_kernprof():
    """
    Test that explicit profiling works when using kernprof. In this case
    we should get as many output files.
    """
    temp_dpath = pathlib.Path(tempfile.mkdtemp())

    with ChDir(temp_dpath):

        script_fpath = pathlib.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())

        args = [sys.executable, '-m', 'kernprof', '-l', os.fspath(script_fpath)]
        print(f'args={args}')
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              universal_newlines=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    assert not (temp_dpath / 'profile_output.txt').exists()
    assert (temp_dpath / 'script.py.lprof').exists()
    shutil.rmtree(temp_dpath)


def test_explicit_profile_with_in_code_enable():
    """
    Test that the user can enable the profiler explicitly from within their
    code.
    """
    temp_dpath = pathlib.Path(tempfile.mkdtemp())

    code = textwrap.dedent(
        '''
        from line_profiler import profile

        @profile
        def func1(a):
            return a + 1

        profile.enable(output_prefix='custom_output')

        @profile
        def func2(a):
            return a + 1

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
        ''').strip()
    with ChDir(temp_dpath):

        script_fpath = pathlib.Path('script.py')
        script_fpath.write_text(code)

        args = [sys.executable, os.fspath(script_fpath)]
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              universal_newlines=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    output_fpath = (temp_dpath / 'custom_output.txt')
    raw_output = output_fpath.read_text()

    assert 'func1' not in raw_output
    assert 'func2' in raw_output
    assert 'func3' not in raw_output
    assert 'func4' in raw_output

    assert output_fpath.exists()
    assert (temp_dpath / 'custom_output.lprof').exists()
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
