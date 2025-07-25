import re
from argparse import ArgumentParser, HelpFormatter
from contextlib import nullcontext
from functools import partial
from io import StringIO
from os.path import join
from shlex import split
from sys import executable
import pytest
from line_profiler.cli_utils import add_argument


@pytest.fixture
def parser():
    """
    Argument parser with the following boolean flags:

    -f, -F, --foo -> foo
    -b, --bar -> bar
    -B, --baz -> baz
    --no-spam -> spam (negated)
    --ham -> ham
    -c -> c
    """
    parser = ArgumentParser(
        formatter_class=partial(HelpFormatter,
                                max_help_position=float('inf'),
                                width=float('inf')))
    # Normal boolean flag (w/2 short forms)
    # -> adds 3 actions (long, short, long-negated)
    add_argument(parser, '-f', '-F', '--foo', action='store_true')
    # Boolean flag w/o parenthetical remark in help text
    # -> adds 3 actions (long, short, long-negated)
    add_argument(parser, '-b', '--bar', action='store_true', help='Set `bar`')
    # Boolean flag w/parenthetical remark in help text
    # -> adds 3 actions (long, short, long-negated)
    add_argument(parser, '-B', '--baz',
                 action='store_true', help='Set `baz` (BAZ)')
    # Negative boolean flag
    # -> adds 1 action (long-negated)
    add_argument(parser, '--no-spam',
                 action='store_false', dest='spam', help='Set `spam` to false')
    # Boolean flag w/o short form
    # -> adds 2 actions (long, long-negated)
    add_argument(parser, '--ham', action='store_true', help='Set `ham`')
    # Short-form-only boolean flag
    # -> adds 1 action (short)
    add_argument(parser, '-e',
                 action='store_true', dest='eggs', help='Set `eggs`')
    yield parser


def test_boolean_argument_help_text(parser):
    """
    Test the help texts generated from boolean arguments added by
    `line_profiler.cli_utils.add_argument(action=...)`.
    """
    assert len(parser._actions) == 14  # One extra option from `--help`
    with StringIO() as sio:
        parser.print_help(sio)
        help_text = sio.getvalue()
    matches = partial(re.search, string=help_text, flags=re.MULTILINE)
    assert matches(r'^  --foo \[.*\] +'
                   + re.escape('(Short forms: -f, -F)')
                   + '$')
    assert matches(r'^  --bar \[.*\] +'
                   + re.escape('Set `bar` (Short form: -b)')
                   + '$')
    assert matches(r'^  --baz \[.*\] +'
                   + re.escape('Set `baz` (BAZ; short form: -B)')
                   + '$')
    assert matches(r'^  --no-spam \[.*\] +'
                   + re.escape('Set `spam` to false')
                   + '$')
    assert matches(r'^  --ham \[.*\] +'
                   + re.escape('Set `ham`')
                   + '$')
    assert matches(r'^  -e +'
                   + re.escape('Set `eggs`')
                   + '$')


@pytest.mark.parametrize(
    ('args', 'foo', 'bar', 'baz', 'spam', 'ham', 'eggs', 'expect_error'),
    [('--foo q', *((None,) * 6), True),  # Can't parse `q` into boolean
     ('-fbB'  # Test short-flag concatenation
      ' --ham=',  # Empty string -> set to false
      True, True, True, None, False, None, False),
     ('--foo'  # No-arg -> set to true
      ' --bar=0'  # Falsy arg -> set to false
      ' --no-baz'  # No-arg (negated flag) -> set to false
      ' --no-spam=no'  # Falsy arg (negated flag) -> set to true
      ' --ham=on'  # Truey arg -> set to true
      ' -e',  # No-arg -> set to true
      True, False, False, True, True, True, False)])
def test_boolean_argument_parsing(
        parser, capsys, args, foo, bar, baz, spam, ham, eggs, expect_error):
    """
    Test the handling of boolean flags.
    """
    if expect_error:
        ctx = pytest.raises(SystemExit)
        match_stderr = 'usage: .* error: argument'
    else:
        ctx = nullcontext()
        match_stderr = '^$'
    with ctx:
        result = vars(parser.parse_args(split(args)))
    stderr = capsys.readouterr().err
    assert re.match(match_stderr, stderr, flags=re.DOTALL)
    if expect_error:
        return
    expected = dict(foo=foo, bar=bar, baz=baz, spam=spam, ham=ham, eggs=eggs)
    assert result == expected


def test_cli():
    """
    Test command line interaction with kernprof and line_profiler.

    References:
        https://github.com/pyutils/line_profiler/issues/9

    CommandLine:
        xdoctest -m ./tests/test_cli.py test_cli
    """
    import ubelt as ub
    import tempfile

    # Create a dummy source file
    code = ub.codeblock(
        '''
        @profile
        def my_inefficient_function():
            a = 0
            for i in range(10):
                a += i
                for j in range(10):
                    a += j

        if __name__ == '__main__':
            my_inefficient_function()
        ''')
    with tempfile.TemporaryDirectory() as tmp_dpath:
        tmp_src_fpath = join(tmp_dpath, 'foo.py')
        with open(tmp_src_fpath, 'w') as file:
            file.write(code)

        # Run kernprof on it
        info = ub.cmd(f'kernprof -l {tmp_src_fpath}', verbose=3, cwd=tmp_dpath)
        assert info['ret'] == 0

        tmp_lprof_fpath = join(tmp_dpath, 'foo.py.lprof')
        tmp_lprof_fpath

        info = ub.cmd(f'{executable} -m line_profiler {tmp_lprof_fpath}',
                      cwd=tmp_dpath, verbose=3)
        assert info['ret'] == 0
        # Check for some patterns that should be in the output
        assert '% Time' in info['out']
        assert '7       100' in info['out']


def test_version_agreement():
    """
    Ensure that line_profiler and kernprof have the same version info
    """
    import ubelt as ub
    info1 = ub.cmd(f'{executable} -m line_profiler --version')
    info2 = ub.cmd(f'{executable} -m kernprof --version')

    if info1['ret'] != 0:
        print(f'Error querying line-profiler version: {info1}')

    if info2['ret'] != 0:
        print(f'Error querying kernprof version: {info2}')

    # Strip local version suffixes
    version1 = info1['out'].strip().split('+')[0]
    version2 = info2['out'].strip().split('+')[0]

    if version2 != version1:
        raise AssertionError(
            'Version Mismatch: kernprof and line_profiler must be in sync. '
            f'kernprof.line_profiler = {version1}. '
            f'kernprof.__version__ = {version2}. '
        )


if __name__ == '__main__':
    """
    CommandLine:
        python ~/code/line_profiler/tests/test_cli.py
    """
    test_version_agreement()
