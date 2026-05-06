"""
Tests to make sure that our :py:deco:`pytest.mark.retry` decorator
works.
"""
from __future__ import annotations

import re
import pprint
import textwrap
from collections.abc import Callable, Collection, Generator, Iterable
from dataclasses import dataclass
from functools import partial
from operator import attrgetter
from pathlib import Path
from shutil import rmtree
from typing import Any, Literal, cast
from typing_extensions import Self

import pytest


pytest_plugins = ('pytester',)

_Status = Literal['passed', 'failed', 'skipped']
_RunPytest_Method = Literal[
    'runpytest', 'runpytest_inprocess', 'runpytest_subprocess',
]
_RunPytest = Callable[..., pytest.RunResult]
_RunnerGetter = Callable[[str, str], _RunPytest]


TEST_COUNTERS = """
from __future__ import annotations
from itertools import count
from typing import Literal

import pytest


@pytest.fixture
def func_scoped_counter() -> count:
    return count()


@pytest.fixture(scope='module')
def module_scoped_counter() -> count:
    return count()


@pytest.mark.parametrize(
    ('scope', 'n'),
    [('func', 0),  # This passes
     ('func', 2),  # This passes with 2 retries
     ('func', 6),  # This fails with 3 retries
     ('module', 4),  # This fails with 3 retries (counter now at 3)
     ('module', 5)]  # This passes with 1 retry (counter now at 5)
)
@pytest.mark.retry(3, reset_fixtures=False)
def test_dynamic_fixtures_persisted(
    request: pytest.FixtureRequest, scope: Literal['func', 'module'], n: int,
) -> None:
    '''
    Test counter fixtures that are requested dynamically via the
    ``request`` fixture; function-scoped fixtures persist between
    test retries.
    '''
    counter = request.getfixturevalue(scope + '_scoped_counter')
    assert next(counter) >= n


@pytest.mark.parametrize(
    ('scope', 'n'),
    [('func', 3),  # This passes with 3 retries
     ('func', 4),  # This fails with 3 retries
     ('module', 4),  # This passes (counter now at 6)
     ('module', 9)]  # This passes with 2 retries (counter now at 9)
)
@pytest.mark.retry(3, reset_fixtures=False)
def test_static_fixtures_persisted(
    func_scoped_counter: Iterable[int],
    module_scoped_counter: Iterable[int],
    scope: Literal['func', 'module'],
    n: int,
) -> None:
    '''
    Test counter fixtures that are requested by name; function-scoped
    fixtures persist between test retries.
    '''
    if scope == 'func':
        counter = func_scoped_counter
    else:
        counter = module_scoped_counter
    assert next(counter) >= n


@pytest.mark.parametrize(
    ('scope', 'n'),
    [('func', 0),  # This passes
     ('func', 1),  # This fails with 1 retry
     ('module', 11)]  # This passes with 1 retry (counter now at 11)
)
@pytest.mark.retry  # Counters reset between retries
def test_dynamic_fixtures_reset(
    request: pytest.FixtureRequest, scope: Literal['func', 'module'], n: int,
) -> None:
    '''
    Test counter fixtures that are requested dynamically via the
    ``request`` fixture; function-scoped fixtures are reset between
    test retries.
    '''
    counter = request.getfixturevalue(scope + '_scoped_counter')
    assert next(counter) >= n


@pytest.mark.parametrize(
    ('scope', 'n'),
    [('func', 0),  # This passes
     ('func', 1),  # This fails with 2 retries
     ('module', 14)]  # This passes with 2 retries (counter now at 14)
)
@pytest.mark.retry(2)  # Ditto above
def test_static_fixtures_reset(
    func_scoped_counter: Iterable[int],
    module_scoped_counter: Iterable[int],
    scope: Literal['func', 'module'],
    n: int,
) -> None:
    '''
    Test counter fixtures that are requested by name; function-scoped
    fixtures are reset between test retries.
    '''
    if scope == 'func':
        counter = func_scoped_counter
    else:
        counter = module_scoped_counter
    assert next(counter) >= n
"""
TEST_TEARDOWN = """
from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Generator
from functools import partial
from pathlib import Path

import pytest


@pytest.fixture(scope='module')
def my_temp_dir(pytestconfig: pytest.Config) -> Generator[Path, None, None]:
    path: Path | None = getattr(pytestconfig.option, 'my_temp_dir', None)
    if path is None:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)
    else:
        yield path

@pytest.fixture(scope='module')
def my_log(pytestconfig: pytest.Config) -> Path | None:
    path: Path | None = getattr(pytestconfig.option, 'my_log', None)
    return path


def _tempfile(*args, **kwargs) -> Path:
    handle, path = tempfile.mkstemp(*args, **kwargs)
    try:
        return Path(path)
    finally:
        os.close(handle)


@pytest.fixture
def maketemp(
    my_temp_dir: Path, my_log: Path | None,
) -> Generator[Callable[..., Path], None, None]:
    paths: list[Path] = []

    def _maketemp(*args, **kwargs) -> Path:
        path = _tempfile(*args, **kwargs)
        paths.append(path)
        log(f'created tempfile {path}')
        return path

    log = partial(_log, _maketemp, my_log)
    try:
        yield _maketemp
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
            log(f'removed tempfile {path}')


def _log(maketemp: Any, my_log: Path | None, msg: str) -> None:
    chunks: list[str] = [
        os.environ['PYTEST_CURRENT_TEST'],
        f'maketemp() @ {id(maketemp):#x}',
        msg,
    ]
    msg = ': '.join(chunks)
    print(msg)
    if my_log is None:
        return
    with my_log.open(mode='a') as fobj:
        print(msg, file=fobj)


@pytest.mark.retry(reset_fixtures=True)
def test_with_fixture_reset(
    my_temp_dir: Path, maketemp: Callable[..., Path],
) -> None:
    path = maketemp(dir=my_temp_dir)
    assert False


@pytest.mark.retry(2, reset_fixtures=False)
def test_no_fixture_reset(
    my_temp_dir: Path, maketemp: Callable[..., Path],
) -> None:
    path = maketemp(dir=my_temp_dir)
    assert False
"""
TEST_EXCEPTIONS = """
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest


@pytest.fixture
def items() -> Iterable[Any]:
    return iter(['1', None, '', '-1'])


@pytest.mark.retry(3, reset_fixtures=('foo',))  # Not resetting `items`
def test_all_xc_types(items: Iterable[Any]) -> None:
    '''
    This should pass after 3 retries because the last item fulfills the
    criterion.
    '''
    assert int(next(items)) < 0


@pytest.mark.retry(3, exceptions=AssertionError, reset_fixtures=())
def test_one_xc_type(items: Iterable[Any]) -> None:
    '''
    This should fail after 1 retry because the second item triggers a
    :py:class:`TypeError`.
    '''
    assert int(next(items)) < 0


@pytest.mark.retry(reset_fixtures=False)
@pytest.mark.retry(exceptions=TypeError)
@pytest.mark.retry(exceptions=AssertionError)
def test_two_xc_types(items: Iterable[Any]) -> None:
    '''
    This should fail after 2 retries because the third item triggers a
    :py:class:`ValueError`.

    Note:
        The three decorators stack to give 3 retries and to accept both
        :py:class:`AssertionError` and :py:class:`TypeError`.
    '''
    assert int(next(items)) < 0


@pytest.mark.retry(
    3,
    exceptions=(AssertionError, TypeError, ValueError),
    reset_fixtures=False,
)
def test_three_xc_types(items: Iterable[Any]) -> None:
    '''
    This should pass after 3 retries because the last item fulfills the
    criterion, and the preceding errors are all included in the
    ``exceptions`` argument to the wrapper.
    '''
    assert int(next(items)) < 0
"""
TEST_CONDITIONS = """
from __future__ import annotations

from sys import version_info

import pytest


@pytest.mark.retry(2, condition=(11 % 2))
def test_concrete_positive_condition() -> None:
    '''
    This should fail after 2 retries because its condition is true.
    '''
    raise RuntimeError


@pytest.mark.retry(condition=('a' in 'foo'))
def test_concrete_negative_condition() -> None:
    '''
    This should fail without retries because its condition is false.
    '''
    raise RuntimeError


@pytest.mark.retry(condition='version_info.major >= 3')
def test_dynamic_positive_condition_test_module_globals() -> None:
    '''
    This should fail after 1 retry because the condition evaluates to
    true on the test module's ``globals()``.
    '''
    raise RuntimeError


@pytest.mark.retry(condition='version_info.major < 3')
def test_dynamic_negative_condition_test_module_globals() -> None:
    '''
    This should fail without retries because the condition evaluates to
    false on the test module's ``globals()``.
    '''
    raise RuntimeError


@pytest.mark.retry(condition='foo == 1')
def test_bad_dynamic_condition() -> None:
    '''
    This should fail without retries because the condition cannot be
    evaluated (``NameError: name 'foo' is not defined``).
    '''
    raise RuntimeError('bar')


@pytest.mark.retry(condition='n % 2')
@pytest.mark.parametrize('n', [0, 1, 2])
def test_dynamic_condition_test_params(n: int) -> None:
    '''
    Subtests `[0]` and `[2]` (resp. subtest `[1]`) should fail without
    retries (resp. with 1 retry) because the condition evaluates to
    false (resp. true) on the test's parametrization.
    '''
    raise RuntimeError
"""


@dataclass
class _TestOutcome:
    name: str = ''
    status: _Status = 'passed'
    retries: int = 0

    def subtest(
        self,
        *params: str,
        status: _Status | None = None,
        retries: int | None = None,
    ) -> Self:
        if status is None:
            status = self.status
        if retries is None:
            retries = self.retries
        name = f'{self.name}[{"-".join(params)}]'
        return type(self)(name, status, retries)


@dataclass
class _TestModule:
    """
    Helper object for running a test module.
    """
    name: str
    content: str
    expected_outcomes: dict[str, list[_TestOutcome]]
    pytester: pytest.Pytester
    conftest: str | None = None

    def __post_init__(self) -> None:
        self.content = self._strip(self.content)
        if self.conftest:
            self.conftest = self._strip(self.conftest)

    def run(
        self,
        *args: str,
        check_results: bool = False,
        check_summary: Literal['verbose', 'concise'] | None = None,
        check_warnings: int | None = None,
        runner: _RunPytest_Method = 'runpytest',
        additional_stdout_lines: Collection[str] = (),
        additional_stderr_lines: Collection[str] = (),
    ) -> pytest.RunResult:
        """
        Args:
            *args (str):
                Passed to :py:meth:`pytester.Pytester.runpytest`
            check_results (bool):
                If true, check that the test outcomes are as expected
                using :py:meth:`pytester.Pytester.assert_outcomes`
            check_summary (bool):
                If true, check that the 'retries summary' report section
                is written with the expected content indicating test
                results and number of retries
            check_warnings (int | None):
                If an integer and if ``check_results`` is true, also
                check that the number of captured warnings match
            runner (Literal['runpytest', 'runpytest_inprocess', \
'runpytest_subprocess']):
                The :py:class:`pytest.Pytester` method used to run the
                test module
            additional_stdout_lines, additional_stderr_lines \
(Collection[str]):
                Additional regex patterns (other than the
                automatically-generated ones) to match against the
                output streams

        Returns:
            :py:class:`pytest.RunResult` object returned by the
            :py:class:`pytest.Pytester` method
        """
        tempfiles: list[Path] = []
        tempdirs: list[Path] = []
        try:
            conftests: list[str] = [self._get_proj_conftest().read_text()]
            if self.conftest:
                conftests.append(self.conftest)
            # Create separate conftest.py in nested subdirs to avoid
            # hook-func implementations stepping oer one another
            path = self.pytester.path
            for i, conftest in enumerate(conftests):
                if i:
                    path /= 'nested'
                    path.mkdir()
                    tempdirs.append(path)
                conftest_file = path / 'conftest.py'
                conftest_file.write_text(conftest)
                tempfiles.append(conftest_file)
            module = path / f'{self.name}.py'
            module.write_text(self.content)
            tempfiles.append(module)
            result = getattr(self.pytester, runner)(*args, str(module))
            if check_results:
                self.check_results(result, check_warnings)
            if check_summary is not None:
                if check_summary == 'verbose':
                    checker = self.check_verbose_summary
                else:
                    checker = self.check_concise_summary
                checker(
                    result,
                    stdout=additional_stdout_lines,
                    stderr=additional_stderr_lines,
                )
            return result
        finally:
            for path in tempfiles:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                else:
                    print('Removed temppath', path)
            for path in reversed(tempdirs):
                try:
                    rmtree(path)
                except OSError:
                    pass
                else:
                    print('Removed tempdir', path)

    def check_results(
        self, result: pytest.RunResult, warnings: int | None = None,
    ) -> None:
        counts: dict[_Status, int] = {}
        for outcomes in self.expected_outcomes.values():
            for outcome in outcomes:
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
        result.assert_outcomes(warnings=warnings, **counts)

    def check_verbose_summary(
        self,
        result: pytest.RunResult,
        stdout: Collection[str] = (),
        stderr: Collection[str] = (),
    ) -> None:
        lines: list[str] = []
        counts: dict[_Status, int] = {}
        for outcomes in self.expected_outcomes.values():
            for outcome in outcomes:
                lines.append(
                    f'.*::{re.escape(outcome.name)} +{outcome.status.upper()}',
                )
                if not outcome.retries:
                    continue
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
                lines.append(r'.*{}.*retried {} time{}'.format(
                    re.escape(outcome.name),
                    outcome.retries,
                    '' if outcome.retries == 1 else 's',
                ))
        lines.extend(
            self._format_header(status, n) for status, n in counts.items()
        )
        self._check_lines(result, [*lines, *stdout], stderr)

    def check_concise_summary(
        self,
        result: pytest.RunResult,
        stdout: Collection[str] = (),
        stderr: Collection[str] = (),
    ) -> None:
        lines: list[str] = []
        counts: dict[_Status, int] = {}
        test_names: dict[_Status, dict[str, set[str]]] = {}
        consolidated_names: dict[_Status, set[str]] = {}
        for parent_test, outcomes in self.expected_outcomes.items():
            for outcome in outcomes:
                if outcome.status == 'failed':
                    lines.append(
                        f'{outcome.status.upper()} +'
                        f'.*::{re.escape(outcome.name)}',
                    )
                if not outcome.retries:
                    continue
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
                (
                    test_names
                    .setdefault(outcome.status, {})
                    .setdefault(parent_test, set())
                    .add(outcome.name)
                )

        for status, tests in test_names.items():
            for parent_test, subtests in tests.items():
                names = consolidated_names.setdefault(status, set())
                n = len(subtests)
                if n == 1:
                    names.add(*subtests)
                else:
                    names.add('{} ({} subtest{})'.format(
                        parent_test, n, '' if n == 1 else 's',
                    ))

        self._check_lines(result, [*lines, *stdout], stderr)

        for status, n in counts.items():
            header = self._format_header(status, n)
            names = consolidated_names[status]
            print(f'Expecting line in the output: "{header}: <...>"...')
            print(f'Expecting these names in said line: {names!r}...')
            line = self._find_line(header + ':', str(result.stdout))
            for test_name in names:
                assert test_name in line

    @staticmethod
    def _check_lines(
        result: pytest.RunResult,
        stdout: Collection[str],
        stderr: Collection[str],
    ) -> None:
        for stream, lines in {
            'stdout': list(stdout), 'stderr': list(stderr),
        }.items():
            if not lines:
                continue
            print(f'Expecting these lines in the {stream}: {lines!r}...')
            getattr(result, stream).re_match_lines_random(lines)

    @staticmethod
    def _find_line(pattern: str, text: str) -> str:
        pattern = f'^.*{pattern}.*'
        maybe_match = re.search(pattern, text, re.MULTILINE)
        if not maybe_match:
            raise ValueError(f'Cannot find {pattern!r} in {text!r}')
        return maybe_match.group()

    @staticmethod
    def _format_header(status: _Status, n: int) -> str:
        return '{} test{} {} with retries'.format(
            n, '' if n == 1 else 's', status,
        )

    @staticmethod
    def _get_proj_conftest() -> Path:
        return Path(__file__).parent / 'conftest.py'

    @staticmethod
    def _strip(text: str) -> str:
        return textwrap.dedent(text).strip('\n')


def _identical_items_are_adjacent(items: Iterable[Any]) -> bool:
    """
    Example:
        >>> _identical_items_are_adjacent([])
        True
        >>> _identical_items_are_adjacent([1])
        True
        >>> _identical_items_are_adjacent([1, 10])
        True
        >>> _identical_items_are_adjacent([1, 10, 1])
        False
        >>> _identical_items_are_adjacent('AAcCb')
        True
        >>> _identical_items_are_adjacent('AcCAb')
        False
    """
    past: set[Any] = set()
    sentinel = object()
    last: Any = sentinel
    for item in items:
        if last is not sentinel and last != item:
            past.add(last)
        if item in past:
            return False
        last = item
    return True


@pytest.fixture
def counters_module(
    pytester: pytest.Pytester,
) -> Generator[_TestModule, None, None]:
    dynamic_p = _TestOutcome('test_dynamic_fixtures_persisted').subtest
    static_p = _TestOutcome('test_static_fixtures_persisted').subtest
    dynamic_r = _TestOutcome('test_dynamic_fixtures_reset').subtest
    static_r = _TestOutcome('test_static_fixtures_reset').subtest
    outcomes = {
        'test_dynamic_fixtures_persisted': [
            dynamic_p('func-0'),
            dynamic_p('func-2', retries=2),
            dynamic_p('func-6', status='failed', retries=3),
            dynamic_p('module-4', status='failed', retries=3),
            dynamic_p('module-5', retries=1),
        ],
        'test_static_fixtures_persisted': [
            static_p('func-3', retries=3),
            static_p('func-4', status='failed', retries=3),
            static_p('module-4'),
            static_p('module-9', retries=2),
        ],
        'test_dynamic_fixtures_reset': [
            dynamic_r('func-0'),
            dynamic_r('func-1', status='failed', retries=1),
            dynamic_r('module-11', retries=1),
        ],
        'test_static_fixtures_reset': [
            static_r('func-0'),
            static_r('func-1', status='failed', retries=2),
            static_r('module-14', retries=2),
        ],
    }
    yield _TestModule('test_counters', TEST_COUNTERS, outcomes, pytester)


@pytest.fixture
def teardown_module(
    pytester: pytest.Pytester,
) -> Generator[_TestModule, None, None]:
    yield _TestModule(
        'test_teardown',
        TEST_TEARDOWN,
        {
            'test_no_fixture_reset':
            [_TestOutcome('test_no_fixture_reset', 'failed', 2)],
            'test_with_fixture_reset':
            [_TestOutcome('test_with_fixture_reset', 'failed', 1)],
        },
        pytester,
        conftest="""
        from __future__ import annotations

        from pathlib import Path

        import pytest


        def pytest_addoption(parser: pytest.Parser) -> None:
            parser.addoption(
                '--my-temp-dir',
                type=Path,
                help=f'persisted tempdir location for {__file__!r}',
            )
            parser.addoption(
                '--my-log',
                type=Path,
                help=f'log file location for tempfile creation/deletion',
            )
        """,
    )


@pytest.fixture
def exceptions_module(
    pytester: pytest.Pytester,
) -> Generator[_TestModule, None, None]:
    yield _TestModule(
        'test_exceptions',
        TEST_EXCEPTIONS,
        {
            'test_all_xc_types':
            [_TestOutcome('test_all_xc_types', retries=3)],
            'test_one_xc_type':
            [_TestOutcome('test_one_xc_type', 'failed', 1)],
            'test_two_xc_types':
            [_TestOutcome('test_two_xc_types', 'failed', 2)],
            'test_three_xc_types':
            [_TestOutcome('test_three_xc_types', retries=3)],
        },
        pytester,
    )


@pytest.fixture
def conditions_module(
    pytester: pytest.Pytester,
) -> Generator[_TestModule, None, None]:
    test = partial(_TestOutcome, status='failed')
    param_test_name = 'test_dynamic_condition_test_params'
    param_test = partial(test(param_test_name).subtest, status='failed')
    yield _TestModule(
        'test_conditions',
        TEST_CONDITIONS,
        {
            'test_concrete_positive_condition':
            [test('test_concrete_positive_condition', retries=2)],
            'test_concrete_negative_condition':
            [test('test_concrete_negative_condition')],
            'test_dynamic_positive_condition_test_module_globals':
            [test(
                'test_dynamic_positive_condition_test_module_globals',
                retries=1,
            )],
            'test_dynamic_negative_condition_test_module_globals':
            [test('test_dynamic_negative_condition_test_module_globals')],
            'test_bad_dynamic_condition':
            [test('test_bad_dynamic_condition')],
            param_test_name:
            [param_test('0'), param_test('1', retries=1), param_test('2')],
        },
        pytester,
    )


@pytest.mark.parametrize('verbose', [True, False])
def test_fixture_scoping(counters_module: _TestModule, verbose: bool) -> None:
    """
    Test that the decorator correctly handles scoped fixtures.
    """
    run = partial(counters_module.run, check_results=True, check_warnings=0)
    if verbose:
        run('--verbose', check_summary='verbose')
    else:
        run(check_summary='concise')


def test_fixture_teardown(
    tmp_path_factory: pytest.TempPathFactory, teardown_module: _TestModule,
) -> None:
    """
    Test that the decorator correctly handles teardown for additional
    fixture copies incurred by retries; in particular, superseded
    function-scoped fixtures should be torn down before their
    replacements are set up.
    """
    Stage = Literal['setup', 'call', 'teardown']

    @dataclass
    class LogEntry:
        test: str
        stage: Stage
        fixture_id: int
        msg: str

        @classmethod
        def parse_line(cls, line: str) -> Self:
            test, ident, *remainder = line.split(': ')
            msg = ': '.join(remainder)
            test_match = re.fullmatch(
                r'(.+) +\((setup|call|teardown)\)', test,
            )
            assert test_match
            test, stage = test_match.group(1, 2)
            assert stage in ('setup', 'call', 'teardown')
            ident_match = re.fullmatch(
                r'maketemp\(\) @ 0x([0-9a-f]+)', ident,
            )
            assert ident_match
            fixture_id = int(ident_match.group(1), base=16)
            return cls(test, cast(Stage, stage), fixture_id, msg)

    tempdir = tmp_path_factory.mktemp('my_temp')
    log = tempdir / 'tempfiles.log'
    teardown_module.run(
        '--verbose', f'--my-temp-dir={tempdir}', f'--my-log={log}',
        check_results=True, check_summary='verbose', check_warnings=0,
    )

    # Check that all the tempfiles ahve been wiped
    files = {path.name for path in tempdir.iterdir()}
    assert not (files - {log.name})

    # Check that tempfiles are deleted as soon as the fixture value
    # that created them went obsolete, before the next rerun;
    # we can verify that by checking that the ids of the `makefile()`
    # fixtures appear in contiguous blocks

    # Note: there seems to be a weird corner case where neighboring tests
    # may reuse the same fixture id (see failing job 73520441960 in
    # pipeline 25091142386); probably has to do with object lifetime.
    # So instead of just checking the `fixture_id`, also consult
    # `test`; it suffices to see that WITHIN THE SAME TEST we don't have
    # fixture values stepping over one another
    with log.open() as fobj:
        entries = [LogEntry.parse_line(line.rstrip('\n')) for line in fobj]
    pprint.pprint(entries)
    for fields in ('test', 'stage'), ('test', 'fixture_id'):
        getter = attrgetter(*fields)
        values = [getter(entry) for entry in entries]
        assert _identical_items_are_adjacent(values), (
            f'Inconsistency in {fields} order: {values!r}'
        )


def test_exception_restrictions(exceptions_module: _TestModule) -> None:
    """
    Test that the decorator correctly handles failures owing to
    different exception classes.
    """
    exceptions_module.run(
        '--verbose',
        check_results=True, check_summary='verbose', check_warnings=0,
    )


def test_retry_conditions(conditions_module: _TestModule) -> None:
    """
    Test that the decorator correctly handles retry conditions.
    """
    conditions_module.run(
        '--verbose',
        check_results=True,
        check_summary='verbose',
        check_warnings=0,
        # `test_bad_dynamic_condition()` should have failed with a
        # `_RetryFailure`, listing the error encountered in the last
        # trial and the error encountered when `eval()`-ing the
        # condition
        additional_stdout_lines=[
            'FAILED +.*::test_bad_dynamic_condition - '
            r".*_RetryFailure: +\(RuntimeError: bar\) +"
            r"-> +\(condition: +'foo == 1' +-> +NameError: .*'foo'.*\)"
        ],
    )
