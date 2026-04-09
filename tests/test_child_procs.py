from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import (
    Callable, Collection, Generator, Mapping, Sequence,
)
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent, indent

import pytest
import ubelt as ub
from _pytest.fixtures import FixtureFunctionDefinition

from line_profiler.line_profiler import LineStats


NUM_NUMBERS = 100
NUM_PROCS = 4

EXTERNAL_MODULE_BODY = dedent("""
from __future__ import annotations


def my_external_sum(x: list[int]) -> int:
    result: int = 0  # GREP_MARKER[EXT-INVOCATION]
    for item in x:
        result += item  # GREP_MARKER[EXT-LOOP]
    return result
""").strip('\n')

TEST_MODULE_BODY = dedent(f"""
from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Callable
from multiprocessing import Pool

from my_ext_module import my_external_sum


def my_local_sum(x: list[int]) -> int:
    result: int = 0  # GREP_MARKER[LOCAL-INVOCATION]
    # The reversing is to prevent bytecode aliasing with
    # `my_external_sum()` (see issue #424, PR #425)
    for item in reversed(x):
        result += item  # GREP_MARKER[LOCAL-LOOP]
    return result


def sum_in_child_procs(
    length: int, n: int, my_sum: Callable[[list[int]], int],
) -> int:
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
    parser.add_argument(
        '--local',
        action='store_const',
        dest='my_sum',
        default=my_external_sum,
        const=my_local_sum,
    )
    options = parser.parse_args(args)
    print(sum_in_child_procs(options.length, options.n, options.my_sum))


if __name__ == '__main__':
    main()
""").strip('\n')


@dataclass
class _ModuleFixture:
    path: Path
    monkeypatch: pytest.MonkeyPatch
    dependencies: Collection[_ModuleFixture] = ()

    def install(
        self, *,
        local: bool = False, children: bool = False, deps_only: bool = False,
    ) -> None:
        for dep in self.dependencies:
            dep.install(local=local, children=children)
        if deps_only:
            return
        path = str(self.path.parent)
        if local:
            self.monkeypatch.syspath_prepend(path)
        if children:
            self.monkeypatch.setenv('PYTHONPATH', path, prepend=os.pathsep)

    @property
    def name(self) -> str:
        return self.path.stem


def _module_path_fixture(
    name: str, body: str, **kwargs,
) -> FixtureFunctionDefinition:
    def my_fixture_func() -> Generator[Path, None, None]:
        with TemporaryDirectory() as mydir_str:
            my_dir = Path(mydir_str)
            my_dir.mkdir(exist_ok=True)
            my_module = my_dir / f'{name}.py'
            my_module.write_text(body)
            yield my_module

    my_fixture_func.__name__ = name
    return pytest.fixture(my_fixture_func, **kwargs)


# Only write the files once per test session
_ext_module = _module_path_fixture(
    'my_ext_module', EXTERNAL_MODULE_BODY, scope='session',
)
_test_module = _module_path_fixture(
    'my_test_module', TEST_MODULE_BODY, scope='session',
)


@pytest.fixture
def ext_module(
    _ext_module: Path, monkeypatch: pytest.MonkeyPatch,
) -> Generator[_ModuleFixture, None, None]:
    """
    Yields:
        :py:class:`_ModuleFixture` helper object containing the code at
        :py:data:`EXTERNAL_MODULE_BODY`
    """
    yield _ModuleFixture(_ext_module, monkeypatch)


@pytest.fixture
def test_module(
    _test_module: Path,
    ext_module: _ModuleFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[_ModuleFixture, None, None]:
    """
    Yields:
        :py:class:`_ModuleFixture` helper object containing the code at
        :py:data:`TEST_MODULE_BODY`
    """
    yield _ModuleFixture(_test_module, monkeypatch, [ext_module])


def _run_as_script(
    runner_args: list[str], test_args: list[str], test_module: _ModuleFixture,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + [str(test_module.path)] + test_args
    test_module.install(children=True, deps_only=True)
    return _run_subproc(cmd, **kwargs)


def _run_as_module(
    runner_args: list[str], test_args: list[str], test_module: _ModuleFixture,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + ['-m', test_module.name] + test_args
    test_module.install(children=True)
    return _run_subproc(cmd, **kwargs)


def _run_as_literal_code(
    runner_args: list[str], test_args: list[str], test_module: _ModuleFixture,
    **kwargs
) -> subprocess.CompletedProcess:
    cmd = runner_args + ['-c', test_module.path.read_text()] + test_args
    test_module.install(children=True, deps_only=True)
    return _run_subproc(cmd, **kwargs)


def _run_subproc(
    cmd: Sequence[str] | str,
    /,
    *args,
    env: Mapping[str, str] | None = None,
    **kwargs
) -> subprocess.CompletedProcess:
    """
    Wrapper around :py:func:`subprocess.run` which writes debugging
    output.
    """
    if isinstance(cmd, str):
        cmd_str = cmd
    else:
        cmd_str = shlex.join(cmd)
    print('Command:', cmd_str)
    if env is not None:
        diff: list[str] = []
        for key in set(os.environ).union(env):
            old = os.environ.get(key)
            new = env.get(key)
            if old is not None is new:
                item = f'{old!r} -> (deleted)'
            elif old is None is not new:
                item = f'{new!r} (added)'
            else:
                if old == new:
                    continue
                item = f'{old!r} -> {new!r}'
            diff.append(f'${{{key}}}: {item}')
        if diff:
            print('Env:', indent('\n'.join(diff), '  '), sep='\n')
    print('-- Process start --')
    # Note: somehow `mypy` doesn't agree with simply unpacking the
    # `*args` into `subprocess.run()`...
    status: int | str = '???'
    try:
        proc = subprocess.run(  # type: ignore[call-overload]
            cmd, *args, env=env, **kwargs,
        )
    except Exception:
        status = 'error'
        raise
    else:
        status = proc.returncode
        return proc
    finally:
        print(f'-- Process end (return status: {status})--')


def _run_test_module(
    run_helper: Callable[..., subprocess.CompletedProcess],
    test_module: _ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    runner: str | list[str] = 'kernprof',
    outfile: str | None = None,
    profile: bool = True,
    *,
    profiled_code_is_tempfile: bool = False,
    use_local_func: bool = False,
    nnums: int | None = None,
    nprocs: int | None = None,
    check: bool = True,
    nhits: Mapping[str, int] | None = None,
) -> tuple[subprocess.CompletedProcess, LineStats | None]:
    """
    Returns:
        process_running_the_test_module (subprocess.CompletedProcess):
            Process object
        profliing_stats (LineStats | None):
            Line-profiling stats (where available)
    """
    def check_output(output: str, tag: str, nhits: int) -> None:
        # The line should be preixed with 5 numbers:
        # lineno, nhits, time, time-per-hit, % time
        actual_nhits = 0
        for line in output.splitlines():
            if line.endswith(f'# GREP_MARKER[{tag}]'):
                try:
                    _, n, _, _, _, *_ = line.split()
                    actual_nhits += int(n)
                except Exception:
                    pass
        assert actual_nhits == nhits

    if isinstance(runner, str):
        runner_args: list[str] = [runner]
    else:
        runner_args = list(runner)

    if not profile:
        nhits = None

    if profile and not profiled_code_is_tempfile:
        runner_args.extend(['--prof-mod', str(test_module.path)])
    if nhits is not None:
        # We need `kernprof` to write the profliing results immediately
        # to preserve data from tempfiles (see note below)
        runner_args.append('--view')

    test_args: list[str] = []
    if use_local_func:
        test_args.append('--local')
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

        # Checks:
        # - The result is correctly calculated
        assert proc.stdout.splitlines()[0] == str(nnums * (nnums + 1) // 2)
        # - Profiling results are written to the specified file
        prof_result: LineStats | None = None
        if outfile is None:
            assert not list(Path.cwd().iterdir())
        else:
            assert os.path.exists(outfile)
            assert os.stat(outfile).st_size
            if profile:
                prof_result = LineStats.from_files(outfile)
        # - If we're keeping track, the function is called the expected
        #   number of times and has run the expected # of loops
        #   (Note: we do it by parsing the output of `kernprof -v`
        #   instead of reading the `--outfile`, because if the profiled
        #   code is in a tempfile the profiling data will be dropped in
        #   the written outfile)
        for tag, num in (nhits or {}).items():
            check_output(proc.stdout, tag, num)
    return proc, prof_result


run_module = partial(_run_test_module, _run_as_module)
run_script = partial(_run_test_module, _run_as_script)
run_literal_code = partial(
    _run_test_module, _run_as_literal_code, profiled_code_is_tempfile=True,
)


@pytest.mark.parametrize(
    ('run_func', 'use_local_func',
     'label'),  # Dummy argument to make `pytest` output more legible
    [(run_module, True, 'module-local'), (run_module, False, 'module-ext'),
     (run_script, True, 'script-local'), (run_script, False, 'script-ext')]
    # Python can't pickle things unless they resided in a retrievable
    # location (so not the script supplied by `python -c`)
    + [(run_literal_code, False, 'literal-code-ext')],
)
@pytest.mark.parametrize(
    ('nnums', 'nprocs'), [(None, None), (None, 3), (200, None)],
)
def test_multiproc_script_sanity_check(
    run_func: Callable[..., subprocess.CompletedProcess],
    test_module: _ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    use_local_func: bool,
    nnums: int,
    nprocs: int,
    label: str,
) -> None:
    """
    Sanity check that the test module functions as expected when run
    with vanilla Python.
    """
    run_func(
        test_module, tmp_path_factory,
        runner=sys.executable, profile=False,
        use_local_func=use_local_func,
        nnums=nnums, nprocs=nprocs,
    )


@pytest.mark.parametrize(
    ('run_func',
     'label2'),  # Dummy argument to make `pytest` output more legible
    [(run_module, 'module'),
     (run_script, 'script'),
     (run_literal_code, 'literal-code')]
)
@pytest.mark.parametrize(
    ('runner', 'outfile', 'profile',
     'label'),  # Dummy argument to make `pytest` output more legible
    # This is essentially a no-op since it doesn't actually do
    # line-profiling, but we check that code path for completeness
    [(['kernprof', '-q', '--no-line'], 'out.prof', False, 'cProfile')]
    # Run line profiling with and w/o profiling targets
    + [(['kernprof', '-q', '-l'], 'out.lprof', False,
        'line_profiler-inactive'),
       (['kernprof', '-q', '-l'], 'out.lprof', True,
        'line_profiler-active')],
)
def test_running_multiproc_script(
    run_func: Callable[..., subprocess.CompletedProcess],
    test_module: _ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    runner: str | list[str],
    outfile: str | None,
    profile: bool,
    label: str,
    label2: str,
) -> None:
    """
    Check that `kernprof` can RUN the test module in various contexts
    (`kernprof [...] <path>`, `kernprof [...] -m <module>`, and
    `kernprof [...] -c "code"`).

    Notes:
        - See issue #422 for the original motivation.

        - This test does not test the actual profiling, just the
          execution of the code and presence of profiling data
          thereafter.
    """
    run_func(test_module, tmp_path_factory, runner, outfile, profile)
