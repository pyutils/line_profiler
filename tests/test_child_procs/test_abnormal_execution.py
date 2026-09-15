from __future__ import annotations

import os
import re
import sys
from collections.abc import Collection, Generator
from contextlib import ExitStack
from functools import partial
from multiprocessing import get_all_start_methods
from pathlib import Path
from stat import S_IWUSR, S_IWGRP, S_IWOTH, S_IWRITE
from tempfile import TemporaryDirectory
from textwrap import indent
from types import ModuleType
from typing import ClassVar, Literal

import pytest

from line_profiler._child_process_profiling.cache import LineProfilingCache

from ._test_child_procs_utils import (
    VenvFixture, run_subproc, strip, check_tagged_line_nhits,
)


DEBUG = True
_USE_FRESH_VENV = True


class _write_debug_log:
    def __init__(self, logfile: os.PathLike[str] | str | None = None) -> None:
        self.file = Path(logfile) if logfile else None

    def __enter__(self) -> None:
        pass

    def __exit__(self, *_, **__) -> None:
        if not (self.file and self.file.exists() and DEBUG):
            return
        print('-- Combined debug logs --', file=sys.stderr)
        print(
            indent(self.file.read_text(), '  '), end='', file=sys.stderr,
        )
        print('-- End of debug logs --', file=sys.stderr)


class _revoke_write_access:
    def __init__(self, path: os.PathLike[str] | str) -> None:
        self.path = Path(path)

    def __enter__(self) -> None:
        self._perms: int | None = None
        if not self.path.is_dir():
            return
        if self.path.stat().st_uid != self.pid:
            return

        mode = self.mode
        for bit in self._writability_bits:
            self._make_unwritable(self.path, bit)
        if mode == self.mode:
            return
        self._perms = mode
        if DEBUG:
            print(
                f'Updated {str(self.path)!r}:',
                f'{oct(mode)} -> {oct(self.mode)}',
                '(disabling write)',
            )

    def __exit__(self, *_, **__) -> None:
        if self._perms is None:
            return
        mode = self.mode
        os.chmod(self.path, self._perms)
        if DEBUG:
            print(
                f'Updated {str(self.path)!r}:',
                f'{oct(mode)} -> {oct(self.mode)}',
                '(enabling write)',
            )

    @classmethod
    def _check_usability(cls, preexisting_handle: bool = False) -> bool:
        """
        Sanity check: can we actually make a directory unwritable by
        tempering with the permission bytes? (Seems to vary by
        platform... probably has to do already-open handles and stuff.)
        """
        with TemporaryDirectory() as tmp_:
            tmp = Path(tmp_)
            my_dir = tmp / 'my_dir'
            my_dir.mkdir()
            # Write a file in the directory and later get a reading
            # handle on it; let's see if the preexisting handle prevents
            # perm changes from taking effect
            some_file: Path | None = None
            if preexisting_handle:
                some_file = my_dir / 'some_time.txt'
                some_file.write_text('foo bar')

            with ExitStack() as stack:
                if some_file is not None:
                    stack.enter_context(some_file.open())
                # Within the `_revoke_write_access` context, trying to
                # write a file inside the directory should result in a
                # `PermissionError`
                stack.enter_context(cls(my_dir))
                try:
                    (my_dir / 'other_file.txt').write_text('Lorem ipsum')
                except PermissionError:
                    return True
                else:
                    return False

    @staticmethod
    def _make_unwritable(path: Path, writable_bytes: int) -> None:
        mode = path.stat().st_mode
        mask = mode & writable_bytes
        if mask:
            os.chmod(path, mode - mask)

    @property
    def mode(self) -> int:
        return self.path.stat().st_mode

    @property
    def pid(self) -> int:
        try:
            return self._pid
        except AttributeError:  # First call
            pass
        try:
            pid = os.getuid()
        except AttributeError:  # Windows
            with TemporaryDirectory() as tmp:
                pid = os.stat(tmp).st_uid
        # Cache on the class
        type(self)._pid = pid
        return pid

    # This will be set when accessing `.pid` (see above)
    _pid: ClassVar[int]
    # Note: `S_IWRITE` is said to work on Windows but it seems wonky
    # (see GitHub issue python/cpython#101675), and it doesn't seem
    # to work on Linux either...
    _writability_bits: ClassVar[Collection[int]]
    if sys.platform.startswith('win32'):
        _writability_bits = S_IWRITE,
    else:  # POSIX
        _writability_bits = S_IWUSR, S_IWGRP, S_IWOTH


@pytest.mark.parametrize(('trigger_timeout', 'label1'),
                         [(True, 'terminate-proc'), (False, 'happy-path')])
@pytest.mark.parametrize(
    ('suppress_error', 'label2'),
    [(True, 'suppress-error'), (False, 'propagate-error')])
def test_prematurely_terminated_process(
    tmp_path_factory: pytest.TempPathFactory,
    process_test_module_object: ModuleType,
    trigger_timeout: bool,
    suppress_error: bool,
    label1: str, label2: str,
) -> None:
    """
    Check that if a :py:class:`multiprocessing.process.BaseProcess` is
    explicitly and prematurely ended (e.g. ``.terminate()``-ed or
    ``.kill()``-ed, profiling data therein are lost, but it doesn't
    crash the session or cause further loss of profiling data.
    """
    nlocal = 123
    nchild = 10
    delay = .03125
    nhits = {'INVOCATION': 1, 'LOOP': nlocal}
    if trigger_timeout:
        # If the timeout isn't enough, exiting the context in the test
        # module would have invoked `BaseProcess.terminate()`, impairing
        # the collection of profiling data
        timeout: float | None = .125  # Would've needed ~ .3 s
    else:
        timeout = None
        nhits['INVOCATION'] += 1
        nhits['LOOP'] += nchild

    tmp = tmp_path_factory.mktemp('mytemp')
    test_module = tmp / 'test.py'
    out_file = tmp / 'out.lprof'
    debug_log: Path | None = None
    test_module.write_text(strip(f'''
    from __future__ import annotations

    from time import sleep

    from {process_test_module_object.__name__} import Worker


    def my_sum(n: list[int], delay: float = 0.) -> int:
        result: int = 0  # GREP_MARKER[INVOCATION]
        for item in range(1, 1 + n):
            sleep(delay)
            result += item  # GREP_MARKER[LOOP]
        return result


    def main() -> None:
        my_sum({nlocal})
        try:
            with Worker.new(my_sum, args=[{nchild}, {delay}]) as worker:
                worker.get_result({timeout})
        except Exception:
            if not {suppress_error}:
                raise


    if __name__ == '__main__':
        main()
    '''))

    cmd = [
        sys.executable, '-m', 'kernprof',
        '--prof-child-procs',
        '--line-by-line',
        '--view',
        f'--prof-mod={test_module}',
        f'--outfile={out_file}',
    ]
    if DEBUG:
        debug_log = tmp / 'debug.log'
        cmd.append(f'--debug-log={debug_log}')
    cmd.append(str(test_module))

    with _write_debug_log(debug_log):
        proc = run_subproc(cmd, capture_output=True, text=True)

        # Check: process termination per-se shouldn't cause `kernprof`
        # to error out
        assert bool(proc.returncode) == (
            trigger_timeout and not suppress_error
        )
        # Check: collection of profiling data is as expected
        for tag, num in nhits.items():
            check_tagged_line_nhits(proc.stdout, tag, num)


@pytest.mark.parametrize(('corrupt', 'label'),
                         [(True, 'with-corruption'), (False, 'no-corruption')])
def test_corrupted_child_stats_file(
    tmp_path_factory: pytest.TempPathFactory,
    corrupt: bool,
    label: str,
) -> None:
    """
    Check that if a child's stats file is corrupted, the profiling data
    of said child are lost and a warning is issued, but it doesn't crash
    the session or cause further loss of profiling data.
    """
    def between(range: tuple[int, int], x: int) -> bool:
        return min(range) <= x <= max(range)

    nlocal = 123
    nchild = [45, 67, 89, 10]
    if corrupt:
        # Since we assume that there are 2+ child-profiling-stats files
        # and we're corrupting one of them, we should capture SOME but
        # not ALL of the child-process data
        min_invocations = 2
        max_invocations = len(nchild)
        min_loops = nlocal + min(nchild)
        max_loops = nlocal + sum(nchild) - min(nchild)
    else:
        min_invocations = max_invocations = len(nchild) + 1
        min_loops = max_loops = sum(nchild) + nlocal

    tmp = tmp_path_factory.mktemp('mytemp')
    test_module = tmp / 'test.py'
    out_file = tmp / 'out.lprof'
    debug_log: Path | None = None
    test_module.write_text(strip(f'''
    from __future__ import annotations

    import multiprocessing

    from line_profiler._child_process_profiling.cache import (
        LineProfilingCache,
    )


    def my_sum(n: list[int]) -> int:
        result: int = 0  # GREP_MARKER[INVOCATION]
        for item in range(1, 1 + n):
            result += item  # GREP_MARKER[LOOP]
        return result


    def main() -> None:
        # Accrue local data
        my_sum({nlocal})

        # Accrue data in child
        with multiprocessing.Pool(2) as pool:
            pool.map(my_sum, {nchild[0::2]!r})

        # Boot up another pool to ensure that we at least have had
        # two worker processes while handled tasks
        with multiprocessing.Pool(2) as pool:
            pool.map(my_sum, {nchild[1::2]!r})

        # Retrieve one of the non-empty profiling-data files and corrupt
        # it
        if {corrupt}:
            cache = LineProfilingCache.load()
            stats, _, *__ = [
                path for path in cache._get_profiling_outfiles()
                if path.stat().st_size
            ]
            stats.write_bytes(b'foo bar baz')


    if __name__ == '__main__':
        main()
    '''))

    cmd = [
        sys.executable, '-m', 'kernprof',
        '--prof-child-procs',
        '--line-by-line',
        '--view',
        f'--prof-mod={test_module}',
        f'--outfile={out_file}',
    ]
    if DEBUG:
        debug_log = tmp / 'debug.log'
        cmd.append(f'--debug-log={debug_log}')
    cmd.append(str(test_module))

    with _write_debug_log(debug_log):
        # Check: data corruption shouldn't cause `kernprof` to error out
        proc = run_subproc(cmd, capture_output=True, text=True, check=True)
        # Check: collection of profiling data is as expected
        for tag, nhits_range in [
            ('INVOCATION', (min_invocations, max_invocations)),
            ('LOOP', (min_loops, max_loops)),
        ]:
            check_tagged_line_nhits(
                proc.stdout, tag, nhits_range, comparator=between,
            )
        # Check: warning for the corrupted file
        if corrupt:
            assert re.search(
                r'UserWarning: .*1 file\(s\) .*cannot be loaded', proc.stderr,
            )


@pytest.fixture(scope='module')
def venv() -> Generator[VenvFixture, None, None]:
    """
    Fresh virtual env with :py:mod:`line_profiler` installed from
    source.
    """
    for venv in VenvFixture._fixture_helper(verbose=True):
        repo_dir = str(Path(__file__).parent.parent.parent)
        venv.run_pip(['install', repo_dir], check=True)
        yield venv


@pytest.mark.parametrize('start_method', ['spawn', 'fork', 'forkserver'])
@pytest.mark.parametrize(
    ('make_unwritable', 'label'),
    [(True, 'cannot-write-pth'), (False, 'can-write-pth')])
@pytest.mark.parametrize(('n', 'nprocs'), [(100, 1)])
def test_unwritable_purelib_path(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    pool_test_module_object: ModuleType,
    make_unwritable: bool,
    start_method: Literal['spawn', 'fork', 'forkserver'],
    n: int,
    nprocs: int,
    label: str,
) -> None:
    """
    Check that if we can't write a .pth file, the profiling data of
    child processes are lost, but it doesn't crash the session or cause
    further loss of profiling data.
    """
    if make_unwritable:
        for condition, success_note, preexisting in [
            ('at all', 'no open child', False),
            ('when a child is open', 'with open child', True),
        ]:
            if _revoke_write_access._check_usability(preexisting):
                if DEBUG:
                    print(
                        f'Writing to a directory ({success_note}) '
                        'successfully prevented',
                    )
                continue
            pytest.skip(
                reason=f'Cannot prevent writing to a directory {condition} '
                'by unsetting the writability bits on this platform '
                f'({sys.platform})',
            )

    get_pth_locs = LineProfilingCache._enumerate_pth_installation_locations

    # With start methods other than `fork`, we can't extend profiling
    # into the worker without writing a .pth
    nhits = {'LOCAL-INVOCATION': 1, 'LOCAL-LOOP': nprocs}
    if start_method not in get_all_start_methods():
        pytest.skip(
            f'start method {start_method!r} not available on '
            f'the platform {sys.platform!r}'
        )
    if start_method == 'fork' or not make_unwritable:
        nhits['LOCAL-INVOCATION'] += nprocs
        nhits['LOCAL-LOOP'] += n

    tmp = tmp_path_factory.mktemp('mytemp')
    module_name = pool_test_module_object.__name__
    out_file = tmp / 'out.lprof'
    debug_log: Path | None = None
    cmd = [
        sys.executable, '-m', 'kernprof',
        '--prof-child-procs',
        '--line-by-line',
        '--view',
        f'--prof-mod={module_name}',
        f'--outfile={out_file}',
    ]
    if DEBUG:
        debug_log = tmp / 'debug.log'
        cmd.append(f'--debug-log={debug_log}')
    cmd.extend([
        '-m',
        module_name,
        f'--start-method={start_method}',
        '-l', str(n),
        '-n', str(nprocs),
        '--local',
    ])

    with _write_debug_log(debug_log):
        # Check: even if we can't write a .pth, it shouldn't cause
        # `kernprof` to error out
        if _USE_FRESH_VENV:
            venv: VenvFixture = request.getfixturevalue('venv')
            pth_locs: list[os.PathLike[str] | str] = venv.eval(
                f'list({get_pth_locs.__qualname__}())',
                {(LineProfilingCache.__module__, 'LineProfilingCache'): None},
            )
            run_kernprof = partial(venv.run_python, cmd[1:])
        else:
            pth_locs = list(get_pth_locs())
            run_kernprof = partial(run_subproc, cmd)
        with ExitStack() as stack:
            if make_unwritable:
                for path in pth_locs:
                    stack.enter_context(_revoke_write_access(path))
            proc = run_kernprof(capture_output=True, text=True, check=True)
        # Check: collection of profiling data is as expected
        for tag, num in nhits.items():
            check_tagged_line_nhits(proc.stdout, tag, num)
        # Check: warning for unwritable .pth locs
        if make_unwritable:
            assert re.search(
                r'UserWarning: .*cannot write a \.pth file', proc.stderr,
            )
