import os
import re
import shlex
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from sys import stderr
from tempfile import TemporaryDirectory

import pytest
import ubelt as ub

from line_profiler import load_stats


class tempdir:
    """
    Example:
        >>> with tempdir() as td:
        ...     assert td.is_dir()
        ...     assert td.samefile('.')
        ...
        >>> assert not td.is_dir()
    """
    def __init__(self, *args, **kwargs):
        self._get_tmpdir = partial(TemporaryDirectory, *args, **kwargs)
        self._stacks = []

    def __enter__(self):  # type: () -> Path
        stack = ExitStack()
        stack.__enter__()
        tmpdir = Path(stack.enter_context(self._get_tmpdir()))
        stack.enter_context(ub.ChDir(tmpdir))
        self._stacks.append(stack)
        return tmpdir

    def __exit__(self, *_, **__):
        self._stacks.pop().close()


class _TestIPython:
    @staticmethod
    def _get_ipython_instance():
        try:
            from IPython.testing.globalipapp import get_ipython
        except ImportError:
            pytest.skip(reason='no `IPython`')
        return get_ipython()

    @staticmethod
    def _emit(request, /, *args, **kwargs):
        config = request.getfixturevalue('pytestconfig')
        # Only emit the messages when we're not capturing
        if config.getoption('capture') in (False, 'no'):
            print(*args, **kwargs)


class TestLPRun(_TestIPython):
    """
    CommandLine:
        pytest -k "TestLPRun and not TestLPRunAll" -s -v
    """
    @pytest.mark.parametrize('modules', [None, 'calendar'])
    def test_lprun_profiling_targets(self, request, modules):
        """ Test ``%%lprun`` with the ``-m`` flag.
        """
        mods = shlex.split(modules or '')
        if mods:
            chunks = []
            for mod in mods:
                chunks.extend(['-m', mod])
            more_flags = shlex.join(chunks)
        else:
            more_flags = None
        lprof = self._test_lprun(request, more_flags)

        # Check the profiling of functions
        # - from the `-f` flag
        assert any(getattr(func, '__name__', None) == 'func'
                   for func in lprof.functions)
        # - from the `-m` flag
        for mod in mods:
            assert any(
                (getattr(func, '__module__', '') == mod
                 or getattr(func, '__module__', '').startswith(mod + '.'))
                for func in lprof.functions)

    @pytest.mark.parametrize(
        ('output', 'text'),
        [(None, None), ('myprof.txt', True), ('myprof.lprof', False)])
    def test_lprun_file_io(self, request, output, text):
        """ Test ``%%lprun`` with the ``-D`` and ``-T`` flags.
        """
        with tempdir() as tmpdir:
            if output:
                out_path = tmpdir / output
                more_flags = shlex.join(['-' + ('T' if text else 'D'),
                                         str(out_path)])
            else:
                more_flags = None
            self._test_lprun(request, more_flags)

            # Check the output files (`-D` or `-T`)
            if output:
                assert out_path.exists()
                assert out_path.stat().st_size
                if not text:  # Test roundtripping
                    load_stats(out_path)
            else:
                assert not os.listdir(tmpdir)

    @pytest.mark.parametrize('bad', [True, False])
    def test_lprun_timer_unit(self, request, bad):
        """ Test ``%%lprun`` with the ``-u`` flag.
        """
        capsys = request.getfixturevalue('capsys')
        if bad:  # Test invalid value
            with pytest.raises(TypeError):
                self._test_lprun(request, '-u not_a_number')
            return
        else:
            unit = 1E-3
            self._test_lprun(request, f'-u {unit}')

        out = capsys.readouterr().out
        # Check the timer (`-u`)
        pattern = re.compile(r'Timer unit:\s*([^\s]+)\s*s')
        match, = (
            m for m in (pattern.match(line) for line in out.splitlines())
            if m)
        assert pytest.approx(float(match.group(1))) == unit

    @pytest.mark.parametrize('skip_zero', [None, '-s', '-z'])
    def test_lprun_skip_zero(self, request, skip_zero):
        """ Test ``%%lprun`` with the ``-s`` and ``-z`` flags.
        """
        capsys = request.getfixturevalue('capsys')
        # Throw in an unrelated module, whose timings are always zero
        more_flags = '-m calendar'
        if skip_zero:
            more_flags = f'{more_flags} {skip_zero}'
        self._test_lprun(request, more_flags)

        # Check whether zero entries are skipped
        out = capsys.readouterr().out

        match = re.search(
            r'^File:\s*.*{}calendar\.py'.format(re.escape(os.sep)),
            out,
            flags=re.MULTILINE)
        assert bool(match) == (not skip_zero)

    @pytest.mark.parametrize(('xc', 'raised'),
                             [(SystemExit(0), False),
                              (ValueError('foo'), True)])
    def test_lprun_exception_handling(self, capsys, xc, raised):
        ip = self._get_ipython_instance()
        ip.run_line_magic('load_ext', 'line_profiler')
        xc_repr = '{}({})'.format(
            type(xc).__name__, ', '.join(repr(a) for a in xc.args))
        ip.run_cell(
            raw_cell=f'func = lambda: (_ for _ in ()).throw({xc_repr})')

        if raised:  # Normal excepts should be bubbled up
            with pytest.raises(type(xc)):
                ip.run_line_magic('lprun', '-f func func()')
            return

        # Special expressions are captured and relegated to a warning
        # message
        ip.run_line_magic('lprun', '-f func func()')
        out = capsys.readouterr().out
        assert f'*** {type(xc).__name__} exception caught' in out

    def _test_lprun(self, request, more_flags):
        ip = self._get_ipython_instance()
        ip.run_line_magic('load_ext', 'line_profiler')
        ip.run_cell(raw_cell='def func():\n    return 2**20')
        command = '-r -f func func()'
        if more_flags:
            command = f'{more_flags} {command}'
        lprof = ip.run_line_magic('lprun', command)

        # Check the recorded timings
        filtered_timings = {
            (filename, lineno, funcname): entries
            for (filename, lineno, funcname), entries
            in lprof.get_stats().timings.items()
            if filename.startswith('<ipython')
            if filename.endswith('>')}
        assert len(filtered_timings) == 1  # 1 function

        func_data, lines_data = next(iter(filtered_timings.items()))
        self._emit(request, f'func_data={func_data}')
        self._emit(request, f'lines_data={lines_data}', file=stderr)
        assert func_data[1] == 1  # lineno of the function
        assert func_data[2] == "func"  # function name
        assert len(lines_data) == 1  # 1 line of code
        assert lines_data[0][0] == 2  # lineno
        assert lines_data[0][1] == 1  # hits

        return lprof


class TestLPRunAll(_TestIPython):
    def test_lprun_all_autoprofile(self):
        """ Test ``%%lprun_all`` without the ``-p`` flag.
        """
        ip = self._get_ipython_instance()
        ip.run_line_magic('load_ext', 'line_profiler')
        lprof = ip.run_cell_magic(
            'lprun_all', line='-r', cell=self.lprun_all_cell_body)
        timings = lprof.get_stats().timings

        # 2 scopes: the module scope and an inner scope (Test.test)
        assert len(timings) == 2

        timings_iter = iter(timings.items())
        func_1_data, lines_1_data = next(timings_iter)
        func_2_data, lines_2_data = next(timings_iter)
        print(f'func_1_data={func_1_data}')
        print(f'lines_1_data={lines_1_data}')
        assert func_1_data[1] == 1  # lineno of the module
        assert len(lines_1_data) == 2  # only 2 lines were executed in this outer scope
        assert lines_1_data[0][0] == 1  # lineno
        assert lines_1_data[0][1] == 1  # hits

        print(f'func_2_data={func_2_data}')
        print(f'lines_2_data={lines_2_data}')
        assert func_2_data[1] == 2  # lineno of the inner function
        assert len(lines_2_data) == 5  # only 5 lines were executed in this inner scope
        assert lines_2_data[1][0] == 4  # lineno
        assert lines_2_data[1][1] == self.loops - 1  # hits

        # Check that the code is executed in the right scope and with
        # the expected side effects
        assert isinstance(ip.user_ns.get("Test"), type)
        assert ip.user_ns['z'] is None

    def test_lprun_all_autoprofile_toplevel(self):
        """ Test ``%%lprun_all`` with the ``-p`` flag.
        """
        ip = self._get_ipython_instance()
        ip.run_line_magic('load_ext', 'line_profiler')
        lprof = ip.run_cell_magic(
            'lprun_all', line='-r -p', cell=self.lprun_all_cell_body)
        timings = lprof.get_stats().timings

        # 1 scope: the module scope
        assert len(timings) == 1

        timings_iter = iter(timings.items())
        func_data, lines_data = next(timings_iter)
        print(f'func_data={func_data}')
        print(f'lines_data={lines_data}')
        assert func_data[1] == 1  # lineno of the module
        assert len(lines_data) == 2  # only 2 lines were executed in this outer scope
        assert lines_data[0][0] == 1  # lineno
        assert lines_data[0][1] == 1  # hits

        # Check that the code is executed in the right scope and with
        # the expected side effects
        assert isinstance(ip.user_ns.get("Test"), type)
        assert ip.user_ns['z'] is None

    def test_lprun_all_timetaken(self):
        """ Test ``%%lprun_all`` with the ``-t`` flag.
        """
        ip = self._get_ipython_instance()
        ip.run_line_magic('load_ext', 'line_profiler')
        result = ip.run_cell_magic(
            'lprun_all', line='-t', cell=self.lprun_all_cell_body)
        assert result is None  # No `-r` flag -> profiler not returned

        # Check that the code is executed in the right scope and with
        # the expected side effects
        assert isinstance(ip.user_ns.get("Test"), type)
        assert ip.user_ns['z'] is None
        # Check that the elapsed time is written to the right scope
        assert ip.user_ns.get("_total_time_taken", None) is not None

    # This example has 2 scopes
    # - The top level (module) scope, and
    # - The inner `Test.test()` (method) scope
    # when the `-p` flag is passed, the inner level shouldn't be
    # profiled
    loops = 20000
    lprun_all_cell_body = f"""
    class Test:
        def test(self):
            loops = {loops}
            for x in range(loops):
                y = x
                if x == (loops - 2):
                    break
    z = Test().test()
    """
