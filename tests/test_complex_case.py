def test_complex_example():
    import sys
    import pathlib
    import subprocess
    import os

    try:
        test_dpath = pathlib.Path(__file__).parent
    except NameError:
        # for development
        test_dpath = pathlib.Path('~/code/line_profiler/tests').expanduser()

    complex_fpath = test_dpath / 'complex_example.py'

    from subprocess import PIPE
    proc = subprocess.run([sys.executable, os.fspath(complex_fpath)], stdout=PIPE,
                          stderr=PIPE, universal_newlines=True)
    print(proc.stdout)
    print(proc.stderr)
    print(proc.returncode)
    proc.check_returncode()
