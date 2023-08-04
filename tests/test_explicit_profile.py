import tempfile
import pathlib
import contextlib
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
    with contextlib.chdir(temp_dpath):

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

    with contextlib.chdir(temp_dpath):

        script_fpath = pathlib.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())

        args = [sys.executable, os.fspath(script_fpath)]
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              env={'LINE_PROFILE': '1'},
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

    with contextlib.chdir(temp_dpath):

        script_fpath = pathlib.Path('script.py')
        script_fpath.write_text(_demo_explicit_profile_script())

        args = [sys.executable, os.fspath(script_fpath)]
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              env={'LINE_PROFILE': '0'},
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

    with contextlib.chdir(temp_dpath):

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
