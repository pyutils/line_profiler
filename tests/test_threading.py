"""
Test that our patch for :py:mod:`threading` is robust.
"""
from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from collections.abc import Callable, Collection, Generator
from contextlib import ExitStack
from functools import partial
from inspect import getsourcelines, unwrap
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent, indent
from threading import Condition, Event, Thread
from types import FunctionType, ModuleType
from typing import Any, ClassVar, Literal, cast
from uuid import uuid4

import pytest

from line_profiler._line_profiler import label as code_to_timing_key
from line_profiler._threading_patches import apply as apply_patches
from line_profiler.line_profiler_utils import restore
from line_profiler.cleanup import Cleanup
from line_profiler.curated_profiling import CuratedProfilerContext
from line_profiler.line_profiler import LineProfiler, LineStats


LOOP_BODY_LINE: int = 0  # Placeholder to appease the linter
N = 2000


@pytest.fixture(scope='module')
def _my_module() -> Generator[Path, None, None]:
    module_text = dedent("""
    from __future__ import annotations

    from contextlib import nullcontext
    from os import PathLike


    def func(n: int, file: str | PathLike[str] | None = None) -> None:
        with (
            nullcontext(None) if file is None else open(file, mode='w')
        ) as fobj:
            for i in range(1, n + 1):
                print(i, file=fobj)
    """).strip('\n')

    global LOOP_BODY_LINE
    LOOP_BODY_LINE = len(module_text.splitlines())

    with TemporaryDirectory() as tmpdir:
        module = Path(tmpdir) / (
            next(propose_module_name('my_module')) + '.py'
        )
        module.write_text(module_text)
        yield module


@pytest.fixture
def my_module_path(_my_module: Path) -> Generator[Path, None, None]:
    with pytest.MonkeyPatch.context() as mp:
        tmpdir = _my_module.parent
        mp.syspath_prepend(str(tmpdir))
        mp.setenv('PYTHONPATH', str(tmpdir), os.pathsep)

        yield _my_module


@pytest.fixture
def my_module_obj(my_module_path: Path) -> Generator[ModuleType, None, None]:
    module = my_module_path.stem
    with restore.mapping(sys.modules, [module]):
        sys.modules.pop(module, None)
        importlib.invalidate_caches()

        yield importlib.import_module(module)


@pytest.fixture
def prof() -> Generator[LineProfiler, None, None]:
    try:
        prof = LineProfiler()
        yield prof
    finally:
        reset_enable_count(prof)


def propose_module_name(
    base: str, existing: Collection[str] | None = None,
) -> Generator[str, None, None]:
    if existing is None:
        existing = sys.modules
    if base not in existing:
        yield base
    while True:
        uuid = str(uuid4()).replace('-', '_')
        name = f'{base}_{uuid}'
        if name not in existing:
            yield name


def reset_enable_count(prof: LineProfiler, count: int = 0) -> None:
    delta = count - cast(int, getattr(prof, 'enable_count', None))
    if delta > 0:
        for _ in range(delta):
            prof.enable_by_count()
    elif delta < 0:
        for _ in range(-delta):
            prof.disable_by_count()
    assert getattr(prof, 'enable_count', None) == count


def get_nhits(stats: LineStats, func: FunctionType, lineno: int) -> int:
    timings: list[tuple[int, int, int]]
    timings = stats.timings.get(code_to_timing_key(func.__code__), [])
    return {lineno: nhits for lineno, nhits, _ in timings}.get(lineno, 0)


@pytest.mark.parametrize('count_at_creation', [0, 1, 2])
@pytest.mark.parametrize('count_at_start', [0, 1, 2])
@pytest.mark.parametrize('use_wrapper', [True, False])
@pytest.mark.parametrize('use_curated_ctx', [True, False])
def test_child_thread_profiling_toggle_by_count(
    my_module_obj: ModuleType,
    prof: LineProfiler,
    count_at_creation: int,
    count_at_start: int,
    use_wrapper: bool,
    use_curated_ctx: bool,
) -> None:
    """
    Test the profiling of child threads when the
    :py:attr:`LineProfiler.enable_count` has been manipulated in
    different manners.
    """
    n = 10
    target = my_module_obj.func
    if use_wrapper:
        target = prof(target)
        is_profiling = True
    else:
        prof.add_callable(target)
        is_profiling = count_at_start > 0

    with ExitStack() as stack:
        if use_curated_ctx:
            stack.enter_context(CuratedProfilerContext(prof))
        else:  # Basically the same, but more explicit
            cleanup = stack.enter_context(Cleanup())
            apply_patches(cleanup, prof)

        reset_enable_count(prof, count_at_creation)
        thread = Thread(target=target, args=(n,))

        reset_enable_count(prof, count_at_start)
        thread.start()
        thread.join()

        nhits = get_nhits(prof.get_stats(), unwrap(target), LOOP_BODY_LINE)
        assert nhits == (n if is_profiling else 0)


@pytest.mark.parametrize('use_wrapper', [True, False])
def test_child_thread_profiling_subclassed(
    my_module_obj: ModuleType, prof: LineProfiler, use_wrapper: bool,
) -> None:
    """
    Test profiling the workload in a :py:class:`Thread` subclass which
    directly overrides the :py:meth:`Thread.run` method.
    """
    target: Callable[[int], None] = my_module_obj.func

    class MyThread(Thread):
        def run(self) -> None:
            self.worker(self.n)

        @staticmethod
        def worker(n: int) -> None:
            target(n)

        @classmethod
        def use(cls) -> None:
            thread = cls()
            thread.start()
            thread.join()

        n: ClassVar[int] = 10

    prof.enable_by_count()
    with CuratedProfilerContext(prof):
        if use_wrapper:
            target = prof(target)
        else:
            prof.add_callable(target)

        MyThread.use()

        nhits = get_nhits(prof.get_stats(), unwrap(target), LOOP_BODY_LINE)
        assert nhits == MyThread.n


@pytest.mark.parametrize('use_wrapper', [True, False])
def test_child_thread_profiling_separate_creation_and_consumption(
    my_module_obj: ModuleType, prof: LineProfiler, use_wrapper: bool,
) -> None:
    """
    Test profiling in a thread whose instantiation and use happened on
    different threads.
    """
    target: Callable[[int], None] = my_module_obj.func

    def construct_thread(cond: Condition, namespace: dict[str, Any]) -> None:
        with cond:
            namespace['thread'] = Thread(target=target, args=(n,))
            cond.notify_all()

    def use_constructed_thread(
        cond: Condition, namespace: dict[str, Any],
    ) -> None:
        with cond:
            cond.wait()
            thread = namespace['thread']
            thread.start()
            thread.join()

    n = 10

    prof.enable_by_count()
    with CuratedProfilerContext(prof):
        if use_wrapper:
            target = prof(target)
        else:
            prof.add_callable(target)

        namespace: dict[str, Any] = {}
        new_thread = partial(Thread, args=(Condition(), namespace))
        constructor_thread = new_thread(target=construct_thread)
        consumer_thread = new_thread(target=use_constructed_thread)
        # Start the consumer first to ensure that the underlying
        # "physical" thread id isn't reused between the two
        consumer_thread.start()
        constructor_thread.start()
        assert (
            None
            is not consumer_thread.ident
            != constructor_thread.ident
            is not None
        )
        constructor_thread.join()
        consumer_thread.join()

        nhits = get_nhits(prof.get_stats(), unwrap(target), LOOP_BODY_LINE)
        assert nhits == n


@pytest.mark.parametrize('lp_core', ['old', 'new'])
def test_child_thread_profiling_in_kernprof(
    tmp_path_factory: pytest.TempPathFactory,
    my_module_obj: ModuleType,
    lp_core: Literal['old', 'new'],
) -> None:
    """
    End-to-end test for profiling multithreaded code with
    :py:mod:`kernprof`.
    """
    if (sys.version_info[:2]) < (3, 12) and lp_core == 'new':
        pytest.skip(
            f"Can't use `sys.monitoring` in Python "
            f'{".".join(str(v) for v in sys.version_info[:3])}'
        )
    tmpdir = tmp_path_factory.mktemp('mytemp')
    module = my_module_obj.__name__

    nfiles = 4
    test_code = dedent(f"""
    from __future__ import annotations

    import os
    from threading import Thread
    from {module} import func


    threads: list[Thread] = []

    for i in range(1, {nfiles + 1}):
        output = os.path.join({str(tmpdir)!r}, f'out-{{i}}.txt')
        threads.append(Thread(target=func, args=(i, output)))

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()
    """).strip('\n')
    stats_file = tmpdir / 'out.lprof'

    cmd = [
        'kernprof', '-l', f'--prof-mod={module}',
        f'--outfile={stats_file}',
        '-c', test_code,
    ]
    env = dict(os.environ)
    env['LINE_PROFILER_CORE'] = lp_core

    # Verify the code execution
    subprocess.run(cmd, check=True, env=env)
    for i in range(1, nfiles + 1):
        out = tmpdir / f'out-{i}.txt'
        assert out.exists()
        assert out.read_text() == ''.join(f'{n}\n' for n in range(1, i + 1))

    # Verify the profiling output
    total_nhits = (nfiles + 1) * nfiles / 2
    assert total_nhits == get_nhits(
        LineStats.from_files(stats_file),
        my_module_obj.func,
        LOOP_BODY_LINE,
    )


def _test_disable_on_one_thread_keeps_other_threads_profiling_scenario(
    nprof: Literal[1, 2], outdir: str | os.PathLike[str] | None = None,
) -> None:
    def work_a() -> int:
        return len([None])  # grep: BODY (a)

    def work_b(n: int = N) -> int:
        total = 0
        for i in range(n):
            total += i  # grep: LOOP (b)
        return total

    if nprof == 1:
        prof_a = prof_b = LineProfiler(work_a, work_b)
    elif nprof == 2:
        prof_a = LineProfiler(work_a)
        prof_b = LineProfiler(work_b)
    else:
        assert False, f'{nprof=}'

    b_ready = Event()
    a_disabled = Event()
    errors = []

    def thread_b() -> None:
        try:
            with prof_b:
                work_b()
                b_ready.set()
                assert a_disabled.wait(timeout=30)
                # Profiling of this thread must survive thread A's
                # disable...
                work_b()
                # ... and exiting the ctx disables `prof_b`, which must
                # not raise
        except BaseException as e:
            errors.append(e)
            b_ready.set()

    # Thread A (the main thread) is the first to enable a profiler and
    # thus the one holding the global registration (when the core is
    # process-global); it bows out while thread B is still profiling.
    with prof_a:
        worker = Thread(target=thread_b)
        worker.start()
        assert b_ready.wait(timeout=30)
        work_a()
    a_disabled.set()
    worker.join(timeout=30)
    assert not worker.is_alive()

    assert not errors, f'thread B errored: {errors!r}'

    # Write the profiling results for the test function to analyze
    if outdir is None:
        path = Path.cwd()
    else:
        path = Path(outdir)
    assert path.is_dir()
    prof_a.dump_stats(path / 'stats_a.lprof')
    prof_b.dump_stats(path / 'stats_b.lprof')


@pytest.mark.parametrize('core', ['sysmon', 'legacy'])
@pytest.mark.parametrize('nprof', [1, 2])
def test_disable_on_one_thread_keeps_other_threads_profiling(
    tmp_path_factory: pytest.TempPathFactory,
    core: Literal['sysmon', 'legacy'],
    nprof: Literal[1, 2],
) -> None:
    """
    Regression tests: disabling a profiler on one thread must not affect
    profilers still active on other threads, be it the same profiler or
    a different one.

    Under the :py:mod:`sys.monitoring` core, registration is
    process-global while the manager bookkeeping is per-thread; the
    first thread's manager used to tear down the global events/tool ID
    as soon as *its* profilers were disabled, silently ending data
    collection for every other thread and making their later
    :py:meth:`LineProfiler.disable` calls raise
    ``ValueError: tool ... is not in use`` from inside user ``finally``
    blocks.

    The core is chosen at import time, so the scenarios run in
    subprocesses (once per core).
    """
    def _get_nhits(
        stats: LineStats, funcname: str, line_pattern: str | re.Pattern,
    ) -> int:
        source, start = getsourcelines(scenario)
        if not isinstance(line_pattern, re.Pattern):
            line_pattern = re.compile(line_pattern)
        try:
            ln_match = start + next(
                offset for offset, line in enumerate(source)
                if line_pattern.search(line.rstrip('\n'))
            )
        except StopIteration:
            return 0

        try:
            (entries_match,) = [
                e for (*_, func), e in stats.timings.items()
                if func == funcname or func.rpartition('.')[-1] == funcname
            ]
        except ValueError:  # != 1 match
            return 0

        return sum(
            nhits for lineno, nhits, _ in entries_match if lineno == ln_match
        )

    tmp = tmp_path_factory.mktemp('mytmp')
    if core == 'sysmon' and sys.version_info[:2] < (3, 12):
        pytest.skip(
            'Cannot use `sys.monitoring` on Python '
            f'{".".join(str(v) for v in sys.version_info[:3])}'
        )
    scenario = (
        _test_disable_on_one_thread_keeps_other_threads_profiling_scenario
    )

    env = {**os.environ, 'LINE_PROFILER_CORE': core}
    script = dedent(f"""
    from runpy import run_path


    func = run_path({__file__!r})[{scenario.__name__!r}]
    func({nprof}, {str(tmp)!r})
    """)
    try:
        proc = subprocess.run(
            [sys.executable, '-c', script],
            capture_output=True, env=env, text=True, timeout=120,
        )
        proc.check_returncode()
    finally:
        for stream in 'stdout', 'stderr':
            fobj = getattr(sys, stream)
            if (content := getattr(proc, stream)):
                content = indent(content, '  ')
                sep = '\n'
            else:
                content, sep = '<n/a>', ' '
            print(f'{stream}:{sep}{content}', end='', file=fobj)

    errors: list[str] = []
    for thread, filename, funcname, pattern, expected in [
        ('Main thread', 'stats_a.lprof', 'work_a', r'# grep: BODY \(a\)$', 1),
        ('Thread B', 'stats_b.lprof', 'work_b', r'# grep: LOOP \(b\)$', 2 * N),
    ]:
        stats = LineStats.from_files(tmp / filename)
        actual = _get_nhits(stats, funcname, pattern)
        if actual != expected:
            errors.append(
                f'{thread}: '
                f'expected {expected} hit(s) on the line {pattern}, '
                f'got {actual}',
            )
    assert not errors
