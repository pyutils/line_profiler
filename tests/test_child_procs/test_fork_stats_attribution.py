"""
Regression tests: profiling data must be attributed to the process that
produced it exactly once.

Forked children inherit the parent profiler's accumulated stats; if they
dump them verbatim, every line the parent executed before the fork is
counted once per forked child when the results are merged (the parent
dumps the same data itself).  See ``LineProfilingCache._wrap_os_fork()``
and ``_StatsHelper.dump()`` for the subtractive-baseline fix these tests
pin down.
"""
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from textwrap import indent
from typing import Literal

import pytest

from line_profiler import LineStats

from ._test_child_procs_utils import strip as code_block


StartMethod = Literal['spawn', 'forkserver', 'fork']
API = Literal['process', 'pool', 'cfut']

LOOP_COUNT = 5000
NUM_TASKS = 4
TIMEOUT = 120

_API_SNIPPETS: dict[API, str] = {
    'process': code_block("""
    procs = [ctx.Process(target=child_work, args=(i,))
             for i in range(num_children)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    """),
    'pool': code_block("""
    with ctx.Pool(2) as pool:
        results = pool.map(child_work, range(num_tasks))
    assert results == [i * 2 for i in range(num_tasks)]
    """),
    'cfut': code_block("""
    import concurrent.futures as cf

    with cf.ProcessPoolExecutor(max_workers=2, mp_context=ctx) as ex:
        results = list(ex.map(child_work, range(num_tasks)))
    assert results == [i * 2 for i in range(num_tasks)]
    """),
}


def _write_workload(
    path: Path, method: StartMethod, api: API, num_children: int = 1,
) -> dict[str, int]:
    blocks = {
        'import': 'import multiprocessing as mp',
        'child_work': code_block("""
        def child_work(x):
            doubled = x * 2
            return doubled
        """),
        'main': code_block(f"""
        def main():
            num_tasks = {NUM_TASKS}
            num_children = {num_children}
            acc = 0
            for i in range({LOOP_COUNT}):
                acc += i
            ctx = mp.get_context({method!r})
        """),
        'guard': code_block("""
        if __name__ == '__main__':
            main()
        """),
    }
    blocks['main'] = '{}\n{}'.format(
        blocks['main'], indent(_API_SNIPPETS[api], '    '),
    )
    source = '\n\n\n'.join(blocks.values())
    path.write_text(source)
    print(
        'Current test: {}'.format(os.environ.get('PYTEST_CURRENT_TEST')),
        'Workload script:\n{}'.format(indent(source, '  ')),
        sep='\n\n',
    )
    lines = source.splitlines()
    return {
        'acc': 1 + lines.index('        acc += i'),
        'child_work': 1 + lines.index('    doubled = x * 2'),
    }


def _run_kernprof(tmp_path: Path, script: Path) -> LineStats:
    """
    Run the real CLI in a real subprocess (the .pth/env machinery only
    fully engages for a fresh interpreter) and load the merged stats.
    """
    outfile = tmp_path / 'out.lprof'
    proc = subprocess.run(
        [sys.executable, '-m', 'kernprof',
         '-l', f'--prof-mod={script}', '--prof-child-procs',
         f'--outfile={outfile}', str(script)],
        cwd=tmp_path, capture_output=True, text=True, timeout=TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return LineStats.from_files(outfile)


def _nhits(stats: LineStats, func_name: str, lineno: int) -> int:
    matches = [
        entry
        for (_, _, func), entries in stats.timings.items()
        if func == func_name
        for entry in entries
        if entry[0] == lineno
    ]
    assert matches, (
        f'no timings recorded for line {lineno} of {func_name}(); '
        f'keys = {sorted(stats.timings)!r}'
    )
    return sum(nhits for _, nhits, _ in matches)


@pytest.mark.parametrize('api', sorted(_API_SNIPPETS))
@pytest.mark.parametrize('method', ['fork', 'forkserver', 'spawn'])
@pytest.mark.usefixtures('check_purelib_dir_writable')
def test_exact_stats_across_start_methods(
    tmp_path: Path, method: StartMethod, api: API,
) -> None:
    """
    The merged profile must show the parent's loop exactly once and the
    children's work exactly ``NUM_TASKS`` times, no matter how the
    children came into existence.
    """
    if method not in multiprocessing.get_all_start_methods():
        pytest.skip(f'start method {method!r} unavailable')
    script = tmp_path / 'workload.py'
    expected_child_hits = 1 if api == 'process' else NUM_TASKS
    linenos = _write_workload(script, method, api)
    stats = _run_kernprof(tmp_path, script)
    assert _nhits(stats, 'main', linenos['acc']) == LOOP_COUNT
    assert (
        _nhits(stats, 'child_work', linenos['child_work'])
        == expected_child_hits
    )


@pytest.mark.usefixtures('check_purelib_dir_writable')
def test_stats_do_not_scale_with_fork_children(tmp_path: Path) -> None:
    """
    Pre-fork parent data must not be re-contributed once per child.
    """
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip("start method 'fork' unavailable")
    script = tmp_path / 'workload.py'
    linenos = _write_workload(script, 'fork', 'process', num_children=3)
    stats = _run_kernprof(tmp_path, script)
    assert _nhits(stats, 'main', linenos['acc']) == LOOP_COUNT
    assert _nhits(stats, 'child_work', linenos['child_work']) == 3
