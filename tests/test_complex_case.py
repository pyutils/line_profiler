

def profile_now(func):
    """
    Wrap a function to print profile information after it is called.

    Args:
        func (Callable): function to profile

    Returns:
        Callable: the wrapped function
    """
    import line_profiler
    profile = line_profiler.LineProfiler()
    new_func = profile(func)

    def wraper(*args, **kwargs):
        try:
            return new_func(*args, **kwargs)
        except Exception:
            pass
        finally:
            profile.print_stats(stripzeros=True)

    wraper.new_func = new_func
    return wraper


def func_to_profile():
    list(range(100))
    tuple(range(100))
    set(range(100))


def test_profile_now():
    func = func_to_profile
    profile_now(func)()


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
