from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Generator, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent, indent

import pytest
import ubelt as ub


NUM_NUMBERS = 100
NUM_PROCS = 4
TEST_MODULE_BODY = dedent(f"""
from __future__ import annotations
from argparse import ArgumentParser
from multiprocessing import Pool


def my_sum(x: list[int]) -> int:
    result: int = 0
    for item in x:
        result += item
    return result


def sum_in_child_procs(length: int, n: int) -> int:
    my_list: list[int] = list(range(1, length + 1))
    sublists: list[list[int]] = []
    subsums: list[int]
    sublength = length // n
    if sublength * n < length:
        sublength += 1
    while my_list:
        sublist, my_list = my_list[:sublength], my_list[sublength:]
        sublists.append(sublist)
    with Pool(n) as pool:
        subsums = pool.map(my_sum, sublists)
        pool.close()
        pool.join()
    return my_sum(subsums)


def main(args: list[str] | None = None) -> None:
    parser = ArgumentParser()
    parser.add_argument('-l', '--length', type=int, default={NUM_NUMBERS})
    parser.add_argument('-n', type=int, default={NUM_PROCS})
    options = parser.parse_args(args)
    print(sum_in_child_procs(options.length, options.n))


if __name__ == '__main__':
    main()
""").strip('\n')


@pytest.fixture(scope='session')
def test_module() -> Generator[Path, None, None]:
    with TemporaryDirectory() as mydir_str:
        my_dir = Path(mydir_str)
        my_dir.mkdir(exist_ok=True)
        my_module = my_dir / 'my_test_module.py'
        with my_module.open('w') as fobj:
            fobj.write(TEST_MODULE_BODY + '\n')
        yield my_module


@pytest.mark.parametrize('as_module', [True, False])
@pytest.mark.parametrize(
    ('nnums', 'nprocs'), [(None, None), (None, 3), (200, None)],
)
def test_multiproc_script_sanity_check(
    test_module: Path,
    tmp_path_factory: pytest.TempPathFactory,
    nnums: int,
    nprocs: int,
    as_module: bool,
) -> None:
    """
    Sanity check that the test module functions as expected when run
    with vanilla Python.
    """
    _run_test_module(
        _run_as_module if as_module else _run_as_script,
        test_module, tmp_path_factory, [sys.executable], None, False,
        nnums=nnums, nprocs=nprocs,
    )


# Note:
# Currently code execution in child processes is not properly profiled;
# these tests are just for checking that `kernprof` doesn't impair the
# proper execution of `multiprocessing` code


fuzz_invocations = pytest.mark.parametrize(
    ('runner', 'outfile', 'profile',
     'label'),  # Dummy argument to make `pytest` output more legible
    [
        (['kernprof', '-q'], 'out.prof', False, 'cProfile'),
        # Run with `line_profiler` with and w/o profiling targets
        (['kernprof', '-q', '-l'], 'out.lprof', False,
         'line_profiler-inactive'),
        (['kernprof', '-q', '-l'], 'out.lprof', True,
         'line_profiler-active'),
    ],
)


@fuzz_invocations
def test_running_multiproc_script(
    test_module: Path,
    tmp_path_factory: pytest.TempPathFactory,
    runner: str | list[str],
    outfile: str | None,
    profile: bool,
    label: str,
) -> None:
    """
    Check that `kernprof` can run the test module as a script
    (`kernprof [...] <path>`).
    """
    _run_test_module(
        _run_as_script,
        test_module, tmp_path_factory, runner, outfile, profile,
    )


@fuzz_invocations
def test_running_multiproc_module(
    test_module: Path,
    tmp_path_factory: pytest.TempPathFactory,
    runner: str | list[str],
    outfile: str | None,
    profile: bool,
    label: str,
) -> None:
    """
    Check that `kernprof` can run the test module as a module
    (`kernprof [...] -m <module>`).
    """
    _run_test_module(
        _run_as_module,
        test_module, tmp_path_factory, runner, outfile, profile,
    )


def _run_as_script(
    runner_args: list[str], test_args: list[str], test_module: Path, **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + [str(test_module)] + test_args
    return subprocess.run(cmd, **kwargs)


def _run_as_module(
    runner_args: list[str],
    test_args: list[str],
    test_module: Path,
    *,
    env: Mapping[str, str] | None = None,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + ['-m', test_module.stem] + test_args
    env_dict = {**os.environ, **(env or {})}
    python_path = env_dict.pop('PYTHONPATH', '')
    if python_path:
        env_dict['PYTHONPATH'] = '{}:{}'.format(
            test_module.parent, python_path,
        )
    else:
        env_dict['PYTHONPATH'] = str(test_module.parent)
    return subprocess.run(cmd, env=env_dict, **kwargs)


def _run_test_module(
    run_helper: Callable[..., subprocess.CompletedProcess],
    test_module: Path,
    tmp_path_factory: pytest.TempPathFactory,
    runner: str | list[str],
    outfile: str | None,
    profile: bool,
    *,
    nnums: int | None = None,
    nprocs: int | None = None,
    check: bool = True,
) -> tuple[subprocess.CompletedProcess, Path | None]:
    """
    Return
    ------
    `(process_running_the_test_module, path_to_profiling_output | None)`
    """
    if isinstance(runner, str):
        runner_args: list[str] = [runner]
    else:
        runner_args = list(runner)
    if profile:
        runner_args.extend(['--prof-mod', str(test_module)])

    test_args: list[str] = []
    if nnums is None:
        nnums = NUM_NUMBERS
    else:
        test_args.extend(['-l', str(nnums)])
    if nprocs is not None:
        test_args.extend(['-n', str(nprocs)])

    with ub.ChDir(tmp_path_factory.mktemp('mytemp')):
        if outfile is not None:
            runner_args.extend(['--outfile', outfile])
        proc = run_helper(
            runner_args, test_args, test_module,
            text=True, capture_output=True,
        )
        try:
            if check:
                proc.check_returncode()
        finally:
            print(f'stdout:\n{indent(proc.stdout, "  ")}')
            print(f'stderr:\n{indent(proc.stderr, "  ")}', file=sys.stderr)

        assert proc.stdout == f'{nnums * (nnums + 1) // 2}\n'

        prof_result: Path | None = None
        if outfile is None:
            assert not list(Path.cwd().iterdir())
        else:
            prof_result = Path(outfile).resolve()
            assert prof_result.exists()
            assert prof_result.stat().st_size
    return proc, prof_result
