import tempfile
import pathlib
import shutil
import sys
import os
import subprocess
import textwrap
from subprocess import PIPE


def test_autoprofile():
    """
    Test that explicit profiling works when using kernprof. In this case
    we should get as many output files.
    """
    temp_dpath = pathlib.Path(tempfile.mkdtemp())

    code = textwrap.dedent(
        '''
        def fib(n):
            a, b = 0, 1
            while a < n:
                a, b = b, a + b

        fib(10)
        ''').strip()

    with ChDir(temp_dpath):
        # os.chdir(temp_dpath)
        script_fpath = pathlib.Path('script.py')
        script_fpath.write_text(code)

        args = [sys.executable, '-m', 'kernprof', '--prof-mod', 'script.py', '-lv', os.fspath(script_fpath)]
        print(f'args={args}')
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              universal_newlines=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        args = [sys.executable, '-m', 'line_profiler', 'script.py.lprof']
        print(f'args={args}')
        proc = subprocess.run(args, stdout=PIPE, stderr=PIPE,
                              universal_newlines=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

    assert not (temp_dpath / 'profile_output.txt').exists()
    assert (temp_dpath / 'script.py.lprof').exists()
    shutil.rmtree(temp_dpath)


class ChDir:
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
        if self._context_dpath is not None:
            os.chdir(self._orig_dpath)
