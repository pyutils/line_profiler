from __future__ import annotations

import enum
import multiprocessing
import os
import shlex
import subprocess
import sys
from collections.abc import (
    Callable, Collection, Generator, Iterable, Mapping, Sequence,
)
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent, indent
from time import monotonic
from typing import Any, Literal, TypeVar, cast, final, overload
from typing_extensions import Self
from uuid import uuid4

import pytest
import ubelt as ub

from line_profiler.line_profiler import LineStats


T = TypeVar('T')
C = TypeVar('C', bound=Callable[..., Any])

NUM_NUMBERS = 100
NUM_PROCS = 4
START_METHODS = set(multiprocessing.get_all_start_methods())


def strip(s: str) -> str:
    return dedent(s).strip('\n')


EXTERNAL_MODULE_BODY = strip("""
from __future__ import annotations


def my_external_sum(x: list[int], fail: bool = False) -> int:
    result: int = 0  # GREP_MARKER[EXT-INVOCATION]
    for item in x:
        result += item  # GREP_MARKER[EXT-LOOP]
    if fail:
        raise RuntimeError('forced failure')
    return result
""")

TEST_MODULE_TEMPLATE = strip("""
from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Callable
from multiprocessing import get_context, Pool
from typing import Literal

from {EXT_MODULE} import my_external_sum


def my_local_sum(x: list[int], fail: bool = False) -> int:
    result: int = 0  # GREP_MARKER[LOCAL-INVOCATION]
    # The reversing is to prevent bytecode aliasing with
    # `my_external_sum()` (see issue #424, PR #425)
    for item in reversed(x):
        result += item  # GREP_MARKER[LOCAL-LOOP]
    if fail:
        raise RuntimeError('forced failure')
    return result


def sum_in_child_procs(
    length: int, n: int, my_sum: Callable[[list[int]], int],
    start_method: Literal['fork', 'forkserver', 'spawn'] | None = None,
    fail: bool = False,
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
    if start_method:
        pool = get_context(start_method).Pool(n)
    else:
        pool = Pool(n)
    with pool:
        subsums = pool.starmap(my_sum, [(sl, fail) for sl in sublists])
        pool.close()
        pool.join()
    return my_sum(subsums, fail)


def main(args: list[str] | None = None) -> None:
    parser = ArgumentParser()
    parser.add_argument('-l', '--length', type=int, default={NUM_NUMBERS})
    parser.add_argument('-n', type=int, default={NUM_PROCS})
    parser.add_argument(
        '-s', '--start-method',
        choices=['fork', 'forkserver', 'spawn'], default=None,
    )
    parser.add_argument('-f', '--force-failure', action='store_true')
    parser.add_argument(
        '--local',
        action='store_const',
        dest='my_sum',
        default=my_external_sum,
        const=my_local_sum,
    )
    options = parser.parse_args(args)
    print(sum_in_child_procs(
        options.length, options.n, options.my_sum,
        start_method=options.start_method,
        fail=options.force_failure,
    ))


if __name__ == '__main__':
    main()
""")


# ============================== Fixtures ==============================


@dataclass
class _ModuleFixture:
    """
    Convenience wrapper around a Python source file which represents an
    importable module.
    """
    path: Path
    monkeypatch: pytest.MonkeyPatch
    dependencies: Collection[_ModuleFixture] = ()

    def install(
        self, *,
        local: bool = False, children: bool = False, deps_only: bool = False,
    ) -> None:
        """
        Set the module at :py:attr:`~.path` up to be importable.

        Args:
            local (bool):
                Make it importable for the CURRENT process (via
                :py:data:`sys.path`).
            children (bool):
                Make it importable for CHILD processes (via
                ``os.environ['PYTHONPATH']``).
            deps_only (bool):
                If true, only does the equivalent setup for
                dependencies.
        """
        for dep in self.dependencies:
            dep.install(local=local, children=children)
        if deps_only:
            return
        path = str(self.path.parent)
        if local:
            self.monkeypatch.syspath_prepend(path)
        if children:
            self.monkeypatch.setenv('PYTHONPATH', path, prepend=os.pathsep)

    @staticmethod
    def propose_name(prefix: str) -> Generator[str, None, None]:
        """
        Propose a valid module name that isn't already occupied.
        """
        while True:
            name = '_'.join([prefix] + str(uuid4()).split('-'))
            if name not in sys.modules:
                assert name.isidentifier()
                yield name

    @property
    def name(self) -> str:
        return self.path.stem


# Only write the files once per test session


@pytest.fixture(scope='session')
def _ext_module() -> Generator[Path, None, None]:
    name = next(_ModuleFixture.propose_name('my_ext_module'))
    with TemporaryDirectory() as mydir_str:
        my_dir = Path(mydir_str)
        my_dir.mkdir(exist_ok=True)
        my_module = my_dir / f'{name}.py'
        my_module.write_text(EXTERNAL_MODULE_BODY)
        yield my_module


@pytest.fixture(scope='session')
def _test_module(_ext_module: Path) -> Generator[Path, None, None]:
    name = next(_ModuleFixture.propose_name('my_test_module'))
    body = TEST_MODULE_TEMPLATE.format(
        EXT_MODULE=_ext_module.stem,
        NUM_NUMBERS=NUM_NUMBERS,
        NUM_PROCS=NUM_PROCS,
    )
    with TemporaryDirectory() as mydir_str:
        my_dir = Path(mydir_str)
        my_dir.mkdir(exist_ok=True)
        my_module = my_dir / f'{name}.py'
        my_module.write_text(body)
        yield my_module


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
        :py:data:`TEST_MODULE_TEMPLATE`
    """
    yield _ModuleFixture(_test_module, monkeypatch, [ext_module])


# ========================== Helper functions ==========================


class _NotSupplied(enum.Enum):
    NOT_SUPPLIED = enum.auto()


class ResultMismatch(ValueError):
    def __init__(
        self,
        expected: Any,
        actual: Any | _NotSupplied = _NotSupplied.NOT_SUPPLIED,
    ) -> None:
        msg = f'expected: {expected}'
        if actual != _NotSupplied.NOT_SUPPLIED:
            msg = f'{msg}, got {actual}'
        super().__init__(msg)
        self.expected = expected
        self.actual = actual

    @property
    def rich_message(self) -> str:
        msg = '{}: {}'.format(type(self).__name__, self.args[0])
        if self.__traceback__ is not None:
            tb = self.__traceback__
            msg = '{}:{}: {}'.format(
                tb.tb_frame.f_code.co_filename, tb.tb_lineno, msg,
            )
        return msg


@final
@dataclass
class _Params:
    """
    Convenience wrapper around :py:func:`pytest.mark.parametrize`.
    """
    params: tuple[str, ...]
    values: list[tuple[Any, ...]]
    defaults: tuple[Any, ...]

    def __post_init__(self) -> None:
        n = len(self.params)
        assert all(p.isidentifier() for p in self.params)  # Validity
        assert len(set(self.params)) == n  # Uniqueness
        assert len(self.defaults) == n  # Consistency
        self.values = list(self._unique(self.values))
        assert all(len(v) == n for v in self.values)

    def __mul__(self, other: Self) -> Self:
        """
        Form a Cartesian product between the two instances with disjoint
        :py:attr:`~.params`, like stacking the
        :py:func:`pytest.mark.parametrize `decorators.

        Example:
            >>> p1 = _Params.new(('a', 'b'), [(0, 0), (1, 2), (3, 4)],
            ...                  defaults=(1, 2))
            >>> p2 = _Params.new('c', [0, 5, 6])
            >>> p1 * p2  # doctest: +NORMALIZE_WHITESPACE
            _Params(params=('a', 'b', 'c'),
                    values=[(0, 0, 0), (0, 0, 5), (0, 0, 6),
                            (1, 2, 0), (1, 2, 5), (1, 2, 6),
                            (3, 4, 0), (3, 4, 5), (3, 4, 6)],
                    defaults=(1, 2, 0))
        """
        assert not set(self.params) & set(other.params)
        return type(self)(
            self.params + other.params,
            [sv + ov for sv in self.values for ov in other.values],
            self.defaults + other.defaults,
        )

    def __add__(self, other: Self) -> Self:
        """
        Concatenate two instances:

        - For parameters appearing in both, their lists of values are
          concatenated.

        - For parameters appearing in either instance, the missing
          values are taken from the other instance's
          :py:attr:`~.defaults`.

        Note:
            In the case of clashes, the :py:attr:`~.defaults` and the
            order of the :py:attr:`~.params` of ``self`` (the left
            operand) take precedence.

        Example:
            >>> p1 = _Params.new(('a', 'b', 'c'),
            ...                  [(0, 0, 0),  # defaults
            ...                   (1, 2, 3), (4, 5, 6)])
            >>> p2 = _Params.new(('c', 'd'), [(7, 8), (9, 10)],
            ...                  defaults=(-1, -1))
            >>> p1 + p2  # doctest: +NORMALIZE_WHITESPACE
            _Params(params=('a', 'b', 'c', 'd'),
                    values=[(0, 0, 0, -1),
                            (1, 2, 3, -1),
                            (4, 5, 6, -1),
                            (0, 0, 7, 8),
                            (0, 0, 9, 10)],
                    defaults=(0, 0, 0, -1))
        """
        self_defaults = dict(zip(self.params, self.defaults))
        other_defaults = dict(zip(other.params, other.defaults))
        new_params = tuple(self._unique(self.params + other.params))

        defaults = {**other_defaults, **self_defaults}
        new_defaults_tuple = tuple(defaults[p] for p in new_params)

        new_values: list[tuple[Any, ...]] = []
        for old_values, old_params in [
            (self.values, self.params), (other.values, other.params),
        ]:
            indices: list[
                tuple[Literal[True], int] | tuple[Literal[False], str]
            ] = [
                (True, old_params.index(p)) if p in old_params else (False, p)
                for p in new_params
            ]
            new_values.extend(
                tuple(
                    (
                        value[cast(int, index)]
                        if available else
                        defaults[cast(str, index)]
                    ) for available, index in indices
                )
                for value in old_values
            )
        return type(self)(new_params, new_values, new_defaults_tuple)

    def __call__(self, func: C) -> C:
        """
        Mark a callable as with :py:func:`pytest.mark.parametrize`.
        """
        # Note: `pytest` automatically assumes single-param values to
        # be unpackes, so comply here
        if len(self.params) == 1:
            marker = pytest.mark.parametrize(
                self.params[0], [v[0] for v in self.values],
            )
        else:
            marker = pytest.mark.parametrize(self.params, self.values)
        return marker(func)

    @staticmethod
    def _unique(items: Iterable[T]) -> Generator[T, None, None]:
        seen: set[T] = set()
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            yield item

    @overload
    @classmethod
    def new(
        cls,
        params: Sequence[str] | str,
        values: Sequence[Sequence[Any]],
        defaults: Sequence[Any] | _NotSupplied = _NotSupplied.NOT_SUPPLIED,
    ) -> Self:
        ...

    @overload
    @classmethod
    def new(
        cls,
        params: str,
        values: Sequence[Any],
        defaults: Any | _NotSupplied = _NotSupplied.NOT_SUPPLIED,
    ) -> Self:
        ...

    @classmethod
    def new(
        cls,
        params: Sequence[str] | str,
        values: Sequence[Sequence[Any]] | Sequence[Any],
        defaults: (
            Sequence[Any] | Any | _NotSupplied
        ) = _NotSupplied.NOT_SUPPLIED,
    ) -> Self:
        """
        Instantiator more akin to :py:func:`pytest.mark.parametrize`:

        - ``params`` can be provided as a comma-separated string

        - Single parameters can be unpacked (singular param-name string
          and param-value sequences)

        - If ``defaults`` are not given, it is implicitly set to the
          FIRST item in ``values``.
        """
        if isinstance(params, str):
            param_list: tuple[str, ...] = tuple(
                p.strip() for p in params.split(',')
            )
            unpacked = len(param_list) == 1
        else:
            param_list = tuple(params)
            unpacked = False
        if defaults == _NotSupplied.NOT_SUPPLIED:
            defaults, *_ = values
        if unpacked:
            default_values: tuple[Any, ...] = defaults,
            value_tuple_list: list[tuple[Any, ...]] = [(v,) for v in values]
        else:
            default_values = tuple(defaults)  # type: ignore[arg-type]
            value_tuple_list = [tuple(v) for v in values]
        return cls(param_list, value_tuple_list, default_values)


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
    proc: subprocess.CompletedProcess | None = None
    time = monotonic()
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
        time = monotonic() - time
        if proc is not None:
            for name, captured, stream in [
                ('stdout', proc.stdout, sys.stdout),
                ('stderr', proc.stderr, sys.stderr),
            ]:
                if captured is None:
                    continue
                print(f'{name}:\n{indent(captured, "  ")}', file=stream)
        print(
            f'-- Process end (time elapsed: {time:.2f} s / '
            f'return status: {status})--'
        )


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
    fail: bool = False,
    start_method: Literal['fork', 'forkserver', 'spawn'] | None = None,
    nnums: int | None = None,
    nprocs: int | None = None,
    check: bool = True,
    nhits: Mapping[str, int] | None = None,
    **kwargs
) -> tuple[subprocess.CompletedProcess, LineStats | None]:
    """
    Returns:
        process_running_the_test_module (subprocess.CompletedProcess):
            Process object
        profliing_stats (LineStats | None):
            Line-profiling stats (where available)
    """
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
    if fail:
        test_args.append('--force-failure')
    if start_method:
        if start_method in START_METHODS:
            test_args.extend(['-s', start_method])
        else:
            pytest.skip(
                f'`multiprocessing` start method {start_method!r} '
                'not available on the platform'
            )
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
            text=True, capture_output=True, check=(check and not fail),
            **kwargs
        )
        # Checks:
        if fail:
            # - The process has failed as expected
            if check:
                assert proc.returncode
        else:
            # - The result is correctly calculated
            expected = nnums * (nnums + 1) // 2
            output_lines = proc.stdout.splitlines()
            if output_lines[0] != str(expected):
                raise ResultMismatch(
                    f'result {expected}', f'output lines: {output_lines}',
                )
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
            _check_output(proc.stdout, tag, num)
    return proc, prof_result


def _check_output(output: str, tag: str, nhits: int) -> None:
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
    if actual_nhits == nhits:
        return
    raise ResultMismatch(
        f'{nhits} hit(s) on line(s) tagged with {tag!r}', actual_nhits,
    )


run_module = partial(_run_test_module, _run_as_module)
run_script = partial(_run_test_module, _run_as_script)
run_literal_code = partial(
    _run_test_module, _run_as_literal_code, profiled_code_is_tempfile=True,
)

# =============================== Tests ================================


def _get_mp_start_method_fuzzer(label_name: str) -> _Params:
    """
    Returns:
        :py:class:`_Params` object which does a full Cartesian-product
        fuzz between ``fail`` (true or false) and ``start_method``
        ('fork', 'forkserver', and 'spawn'; default :py:const:`None`)
    """
    fuzz_fail = _Params.new(('fail', label_name),
                            [(True, 'failure'), (False, 'success')],
                            defaults=(False, 'success'))
    fuzz_start = _Params.new('start_method', ['fork', 'forkserver', 'spawn'],
                             defaults=None)
    return fuzz_fail * fuzz_start


_fuzz_sanity = (
    _Params.new(('run_func', 'label1'),
                [(run_module, 'module'), (run_script, 'script')])
    * _Params.new(('use_local_func', 'label2'),
                  [(True, 'local'), (False, 'ext')])
    # Python can't pickle things unless they resided in a retrievable
    # location (so not the script supplied by `python -c`)
    + _Params.new(('run_func', 'label1', 'use_local_func', 'label2'),
                  [(run_literal_code, 'literal-code', False, 'ext')])
    # Also fuzz the parallelization-related stuff, esp. check what
    # happens if an exception is raised inside the parallelly-run func
    + _get_mp_start_method_fuzzer('label3')
    + _Params.new(('nnums', 'nprocs'), [(200, None), (None, 3)],
                  defaults=(None, None))
)


@_fuzz_sanity
def test_multiproc_script_sanity_check(
    run_func: Callable[..., subprocess.CompletedProcess],
    test_module: _ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    use_local_func: bool,
    fail: bool,
    start_method: Literal['fork', 'forkserver', 'spawn'] | None,
    nnums: int | None,
    nprocs: int | None,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str, label3: str,
) -> None:
    """
    Sanity check that the test module functions as expected when run
    with vanilla Python.
    """
    run_func(
        test_module, tmp_path_factory,
        runner=sys.executable, profile=False,
        fail=fail,
        use_local_func=use_local_func,
        start_method=start_method,
        nnums=nnums, nprocs=nprocs,
    )


@pytest.mark.parametrize(
    ('run_func', 'label1'),
    [(run_module, 'module'),
     (run_script, 'script'),
     (run_literal_code, 'literal-code')]
)
@pytest.mark.parametrize(
    ('runner', 'outfile', 'profile',
     'label2'),  # Dummy argument to make `pytest` output more legible
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
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str,
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


_fuzz_prof_mp_1 = (
    _Params.new(('run_func', 'label1'),
                [(run_module, 'module'),
                 (run_script, 'script'),
                 (run_literal_code, 'literal-code')],
                defaults=(run_script, 'script'))
    + _Params.new(('prof_child_procs', 'label2'),
                  [(True, 'with-child-prof'), (False, 'no-child-prof')])
    + _get_mp_start_method_fuzzer('label3')
)
_fuzz_prof_mp_2 = (
    _Params.new(('preimports', 'label4'),
                [(True, 'with-preimports'), (False, 'no-preimports')],
                defaults=(False, 'no-preimports'))
    + _Params.new(('use_local_func', 'label5'),
                  [(True, 'local'), (False, 'external')],
                  defaults=(False, 'external'))
)


@_fuzz_prof_mp_1
@_fuzz_prof_mp_2
@pytest.mark.parametrize(
    # XXX: should we explicitly test the single-proc case? We already
    # have quite a lot of subtests tho...
    ('nnums', 'nprocs'), [(2000, 3)],
)
def test_profiling_multiproc_script(
    run_func: Callable[..., subprocess.CompletedProcess],
    test_module: _ModuleFixture,
    ext_module: _ModuleFixture,
    tmp_path_factory: pytest.TempPathFactory,
    prof_child_procs: bool,
    preimports: bool,
    use_local_func: bool,
    fail: bool,
    start_method: Literal['fork', 'forkserver', 'spawn'] | None,
    nnums: int,
    nprocs: int,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str, label3: str, label4: str, label5: str,
) -> None:
    """
    Check that `kernprof` can PROFILE the test module in various
    contexts, optionally extending profiling into child processes.

    Note:
        This test function is heavily parametrized. Here is why that is
        necessary:

        - ``run_func`` tests the different :cmd:`kernprof` modes (see
          :py:func:`~.test_running_multiproc_script`).

        - ``preimports`` tests that both mechanisms for setting up
          profiling targets work:

          - :py:const:`True`: child processes import the module
            generated by
            :py:mod:`line_profiler.autoprofile.eager_preimports`, like
            the main :py:mod:`kernprof` process does.

          - :py:const:`False`: child processes rewrite the executed code
            before passing it to :py:mod:`runpy`, similar to what
            :py:mod:`line_profiler.autoprofile.autoprofile` does.

          These code paths go through different
          :py:mod:`multiprocessing` components that we have patched and
          thus needs separate testing.

        - ``use_local_func`` tests that we can consistently set up
          profiling in both functions locally-defined in the profiled
          code and imported by it.

        - ``fail`` tests that our patches and hook doesn't choke when
          exceptions occur in child processes, and profiling data can
          still be collected.

        - ``start_method`` tests whether all available
          :py:mod:`multiprocessing` start methods are covered.

        - ``prof_child_procs`` of course toggles whether to do the
          patches to set up profiling in child processes.
    """
    # XXX: owing to the shenanigans in
    # `line_profiler._child_process_profiling.multiprocessing_patches`,
    # there is a risk that failing child processes are not properly
    # `.terminate()`-ed. So just put in a timeout...
    timeout = 5  # Seconds

    # How many calls do we expect?
    nhits = dict.fromkeys(
        ['EXT-INVOCATION', 'EXT-LOOP', 'LOCAL-INVOCATION', 'LOCAL-LOOP'], 0,
    )
    # Make sure we're profiling the right function
    tag = 'LOCAL' if use_local_func else 'EXT'
    tag_call = tag + '-INVOCATION'
    tag_loop = tag + '-LOOP'
    if not fail:
        # The final sum in the parent process should always be profiled
        # unless the child processes failed and we never returned from
        # `Pool.starmap()`
        nhits[tag_call] += 1
        nhits[tag_loop] += nprocs
    if prof_child_procs:
        # When profiling extends into child processes, each of them
        # invokes the sum function once and when combined they loop thru
        # all the items
        nhits[tag_call] += nprocs
        nhits[tag_loop] += nnums

    runner = ['kernprof', '-l']
    runner.extend([
        '--{}prof-child-procs'.format('' if prof_child_procs else 'no-'),
        '--{}preimports'.format('' if preimports else 'no-'),
    ])
    if not use_local_func:
        # Also make sure to include the external module in `--prof-mod`
        runner.append(f'--prof-mod={ext_module.name}')
    run_func(
        test_module, tmp_path_factory,
        runner=runner,
        outfile='out.lprof',
        profile=True,
        use_local_func=use_local_func,
        fail=fail,
        start_method=start_method,
        nhits=nhits,
        nnums=nnums,
        nprocs=nprocs,
        timeout=timeout,
    )


@pytest.mark.parametrize(('use_subprocess', 'label1'),
                         [(True, 'subprocess.run'), (False, 'os.system')])
@pytest.mark.parametrize(('prof_child_procs', 'label2'),
                         [(True, 'with-child-prof'), (False, 'no-child-prof')])
@pytest.mark.parametrize(('fail', 'label3'),
                         [(True, 'failure'), (False, 'success')])
@pytest.mark.parametrize('n', [200])
def test_profiling_bare_python(
    tmp_path_factory: pytest.TempPathFactory,
    ext_module: _ModuleFixture,
    use_subprocess: bool,
    prof_child_procs: bool,
    fail: bool,
    n: int,
    # Dummy arguments to make `pytest` output more legible
    label1: str, label2: str, label3: str,
) -> None:
    """
    Check that `kernprof` can profile the target functions if the code
    invokes another bare Python process (via either :py:func:`os.system`
    or :py:func:`subprocess.run`) that calls them.
    """
    ext_module.install(children=True)
    temp_dir = tmp_path_factory.mktemp('mytemp')

    script_path = temp_dir / 'my-script.py'
    script_content = strip("""
    from {EXT_MODULE} import my_external_sum


    if __name__ == '__main__':
        numbers = list(range(1, 1 + {N}))
        result = my_external_sum(numbers, {FAIL})
    """.format(
        EXT_MODULE=ext_module.name,
        N=n,
        FAIL=fail,
    ))
    script_path.write_text(script_content)

    out_file = temp_dir / 'out.lprof'
    cmd = [
        'kernprof', '-lv', '--preimports',
        f'--prof-mod={ext_module.name}',
        f'--outfile={out_file}',
        '--{}prof-child-procs'.format('' if prof_child_procs else 'no-'),
    ]
    sub_cmd = [sys.executable, str(script_path)]
    if use_subprocess:
        code = strip(f"""
        import subprocess


        subprocess.run({sub_cmd!r}, check=True)
        """)
    else:
        code = strip("""
        import os


        if os.system({!r}):
            raise RuntimeError('called process failed')
        """.format(shlex.join(sub_cmd)))
    cmd.extend(['-c', code])
    proc = _run_subproc(cmd, text=True, capture_output=True)

    nhits = {'EXT-INVOCATION': 1, 'EXT-LOOP': n}
    if not prof_child_procs:
        for k in nhits:
            nhits[k] = 0

    # Check that the code errors out when expected
    assert bool(fail) == bool(proc.returncode)
    # Check that the profiling output is as expected
    for tag, num in nhits.items():
        _check_output(proc.stdout, tag, num)
