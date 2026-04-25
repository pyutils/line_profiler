"""
Tests to make sure that our :py:deco:`pytest.mark.retry` decorator
works.
"""
from __future__ import annotations

import re
import textwrap
from collections.abc import Callable, Generator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from shutil import rmtree
from typing import Literal
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


def _tempfile(*args, **kwargs) -> Path:
    handle, path = tempfile.mkstemp(*args, **kwargs)
    try:
        return Path(path)
    finally:
        os.close(handle)


@pytest.fixture
def maketemp(
    my_temp_dir: Path,
) -> Generator[Callable[..., Path], None, None]:
    paths: list[Path] = []
    try:
        def _maketemp(*args, **kwargs) -> Path:
            path = _tempfile(*args, **kwargs)
            paths.append(path)
            print(f'maketemp() @ {id(_maketemp):#x}: created tempfile {path}')
            return path

        yield _maketemp
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
            print(f'maketemp() @ {id(_maketemp):#x}: removed tempfile {path}')


@pytest.mark.retry(reset_fixtures=True)
def test_no_fixture_reset(
    my_temp_dir: Path, maketemp: Callable[..., Path],
) -> None:
    path = maketemp(dir=my_temp_dir)
    assert False


@pytest.mark.retry(2, reset_fixtures=False)
def test_with_fixture_reset(
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
                    self.check_verbose_summary(result)
                else:
                    self.check_concise_summary(result)
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

    def check_verbose_summary(self, result: pytest.RunResult) -> None:
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

        print(f'Expecting these lines in the output: {lines!r}...')
        result.stdout.re_match_lines_random(lines)

    def check_concise_summary(self, result: pytest.RunResult) -> None:
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

        print(f'Expecting these lines in the output: {lines!r}...')
        result.stdout.re_match_lines_random(lines)

        for status, n in counts.items():
            header = self._format_header(status, n)
            names = consolidated_names[status]
            print(f'Expecting line in the output: "{header}: <...>"...')
            print(f'Expecting these names in said line: {names!r}...')
            line = self._find_line(header + ':', str(result.stdout))
            for test_name in names:
                assert test_name in line

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
            [_TestOutcome('test_no_fixture_reset', 'failed', 1)],
            'test_with_fixture_reset':
            [_TestOutcome('test_with_fixture_reset', 'failed', 2)],
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
    fixture copies incurred by retries.
    """
    tempdir = tmp_path_factory.mktemp('my_temp')
    print(tempdir)
    teardown_module.run(
        '--verbose', f'--my-temp-dir={tempdir}',
        check_results=True, check_summary='verbose', check_warnings=0,
    )
    files = {path.name for path in tempdir.iterdir()}
    assert not files


def test_exception_restrictions(exceptions_module: _TestModule) -> None:
    """
    Test that the decorator correctly handles failures owing to
    different exception classes.
    """
    exceptions_module.run(
        '--verbose',
        check_results=True, check_summary='verbose', check_warnings=0,
    )
