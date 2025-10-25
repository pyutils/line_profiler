"""
Test the handling of TOML configs.
"""
import os
import pathlib
import platform
import re
import subprocess
import shutil
import sys
import textwrap
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Generator, Sequence, Union

import pytest

from line_profiler.toml_config import ConfigSource


def write_text(path: pathlib.Path, text: str, /, *args, **kwargs) -> int:
    text = textwrap.dedent(text).strip('\n')
    return path.write_text(text, *args, **kwargs)


@pytest.fixture(autouse=True)
def fresh_curdir(monkeypatch: pytest.MonkeyPatch,
                 tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """
    Ensure that the tests start on a clean slate: they shouldn't see
    the environment variable :envvar:`LINE_PROFILER_RC`, nor should the
    :py:meth:`~.ConfigSource.from_config` lookup find any config file.

    Yields:
        curdir (pathlib.Path):
            The temporary directory we `chdir()`-ed into for the test.
    """
    monkeypatch.delenv('LINE_PROFILER_RC', raising=False)
    path = tmp_path_factory.mktemp('clean').absolute()
    monkeypatch.chdir(path)
    yield path


def test_environment_isolation() -> None:
    """
    Test that we have isolated the tests from the environment with the
    :py:func:`fresh_curdir` fixture.
    """
    assert 'LINE_PROFILER_RC' not in os.environ
    assert ConfigSource.from_config() == ConfigSource.from_default()


def test_default_config_deep_copy() -> None:
    """
    Test that :py:meth:`ConfigSource.from_default` always return a fresh
    copy of the default config.
    """
    default_1, default_2 = (
        ConfigSource.from_default().conf_dict for _ in (1, 2))
    assert default_1 == default_2
    assert default_1 is not default_2
    # Sublist
    environ_flags = default_1['setup']['environ_flags']
    assert isinstance(environ_flags, list)
    assert environ_flags == default_2['setup']['environ_flags']
    assert environ_flags is not default_2['setup']['environ_flags']
    # Subtable
    column_widths = default_1['show']['column_widths']
    assert isinstance(column_widths, dict)
    assert column_widths == default_2['show']['column_widths']
    assert column_widths is not default_2['show']['column_widths']


def test_table_normalization(fresh_curdir: pathlib.Path) -> None:
    """
    Test that even if a config file misses some items (and has so extra
    ones), it is properly normalized to contain the same keys as the
    default table.
    """
    default_config = ConfigSource.from_default().conf_dict
    toml = fresh_curdir / 'foo.toml'
    write_text(toml, """
    [unrelated.table]
    foo = 'foo'  # This should be ignored

    [tool.line_profiler.write]
    output_prefix = 'my_prefix'  # This is parsed and retained
    nonexistent_key = 'nonexistent_value'  # This should be ignored
    """)
    loaded = ConfigSource.from_config(toml)
    assert loaded.path.samefile(toml)
    assert loaded.conf_dict['write']['output_prefix'] == 'my_prefix'
    assert 'nonexistent_key' not in loaded.conf_dict['write']
    del loaded.conf_dict['write']['output_prefix']
    del default_config['write']['output_prefix']
    assert loaded.conf_dict == default_config


def test_malformed_table(fresh_curdir: pathlib.Path) -> None:
    """
    Test that we get a `ValueError` when loading a malformed table with a
    non-subtable value taking the place of a supposed subtable.
    """
    toml = fresh_curdir / 'foo.toml'
    write_text(toml, """
    [tool.line_profiler]
    write = [{lprof = true}]  # This shouldn't be a list
    """)
    with pytest.raises(ValueError,
                       match=r"config = .*: expected .* keys.*:"
                       r".*'tool\.line_profiler\.write'"):
        ConfigSource.from_config(toml)


def test_config_lookup_hierarchy(monkeypatch: pytest.MonkeyPatch,
                                 fresh_curdir: pathlib.Path) -> None:
    """
    Test the hierarchy according to which we load config files.
    """
    default = ConfigSource.from_default().path
    # Lowest priority: `pyproject.toml` or `line_profiler.toml` in an
    # ancestral directory
    curdir = fresh_curdir
    lowest_priority = curdir / 'line_profiler.toml'
    lowest_priority.touch()
    curdir = curdir / 'child'
    curdir.mkdir()
    monkeypatch.chdir(curdir)
    assert ConfigSource.from_config().path.samefile(lowest_priority)
    # Higher priority: the same in the current directory
    # (`line_profiler.toml` preferred over `pyproject.toml`)
    lower_priority = curdir / 'pyproject.toml'
    lower_priority.touch()
    assert ConfigSource.from_config().path.samefile(lower_priority)
    low_priority = curdir / 'line_profiler.toml'
    low_priority.touch()
    assert ConfigSource.from_config().path.samefile(low_priority)
    # Higher priority: a file specified by the `${LINE_PROFILER_RC}`
    # environment variable (but we fall back to the default if that
    # fails)
    monkeypatch.setenv('LINE_PROFILER_RC', 'foo.toml')
    assert ConfigSource.from_config().path.samefile(default)
    high_priority = curdir / 'foo.toml'
    high_priority.touch()
    assert ConfigSource.from_config().path.samefile(high_priority)
    # Highest priority: a file passed explicitly to the `config`
    # parameter
    highest_priority = curdir.parent / 'bar.toml'
    with pytest.raises(FileNotFoundError):
        ConfigSource.from_config(highest_priority)
    highest_priority.touch()
    assert (ConfigSource.from_config(highest_priority)
            .path.samefile(highest_priority))
    # Also test that `True` is equivalent to the default behavior
    # (`None`), and `False` to disabling all lookup
    assert ConfigSource.from_config(True).path.samefile(high_priority)
    assert ConfigSource.from_config(False).path.samefile(default)


########################################################################
#           START: edge-case test for `importlib_resources`            #
########################################################################

# XXX: this addresses an edge case where `importlib.resources` have been
# superseded by `importlib_resources` during runtime.
# Do we REALLY need such an involved (and slow) test for something we
# don't otherwise interact with?


@pytest.fixture(scope='module')
def _venv() -> Generator[Path, None, None]:
    """
    A MODULE-scoped fixture for a venv in which `line_profiler` has been
    separately installed.
    """
    with TemporaryDirectory() as tmpdir_:
        tmpdir = Path(tmpdir_)
        # Build the venv
        venv = tmpdir / 'venv'
        cmd = [sys.executable, '-m', 'venv', venv]
        run_proc(cmd).check_returncode()
        # Install `line_profiler` in the venv
        # (somehow `--system-site-packages` doesn't work)
        source = Path(__file__).parent.parent
        install = run_pip_in_venv('install', [source], tmpdir, venv)
        install.check_returncode()
        yield venv


@pytest.fixture
def venv(tmp_path: Path, _venv: Path) -> Generator[Path, None, None]:
    """
    A FUNCTION-scoped fixture for a venv in which:
    - `line_profiler` has been separately installed, and
    - `importlib-resources` is uninstalled after each test.
    """
    try:
        yield _venv
    finally:
        run_pip_in_venv('uninstall', ['--yes', 'importlib-resources'],
                        tmp_path, _venv)


def run_proc(
        cmd: Sequence[Union[str, Path]],
        /,
        **kwargs) -> subprocess.CompletedProcess:
    """Convenience wrapper around `subprocess.run()`."""
    kwargs.update(text=True, capture_output=True)
    proc = subprocess.run([str(arg) for arg in cmd], **kwargs)
    print(proc.stderr, end='', file=sys.stderr)
    print(proc.stdout, end='')
    return proc


def run_python_in_venv(
        args: Sequence[Union[str, Path]],
        tmpdir: Path,
        venv: Path) -> subprocess.CompletedProcess:
    """Run `$ python *args` in `venv`."""
    if 'windows' in platform.system().lower():  # Use PowerShell
        shell = shutil.which('PowerShell.exe')
        assert shell is not None
        script_file = tmpdir / 'script.ps1'
        write_text(script_file, """
        $Activate, $Remainder = $args
        Invoke-Expression $Activate
        python @Remainder
        """)
        base_cmd = [shell, '-NonInteractive', '-File', script_file,
                    venv / 'Scripts' / 'Activate.ps1']
    else:  # Use Bash
        shell = shutil.which('bash')
        assert shell is not None
        script_file = tmpdir / 'script.bsh'
        write_text(script_file, """
        activate="$1"; shift
        source "${activate}"
        python "${@}"
        """)
        base_cmd = [shell, script_file, venv / 'bin' / 'activate']

    return run_proc(base_cmd + list(args))


def run_pip_in_venv(
        subcommand: str,
        arguments: Union[Sequence[Union[str, Path]], None] = None,
        /,
        *args,
        **kwargs) -> subprocess.CompletedProcess:
    cmd = ['-m', 'pip', subcommand, '--require-virtualenv', *(arguments or [])]
    return run_python_in_venv(cmd, *args, **kwargs)


@pytest.mark.parametrize(
    'version',
    [False,           # Don't use `importlib_resources`
     True,            # Newest (`path()` imported from stdlib)
     '< 6',           # Legacy (defines `path()` but deprecates it)
     '>= 6, < 6.4'])  # Corner case (`path()` unavailable)
def test_backported_importlib_resources(
        tmp_path: Path, venv: Path, version: Union[str, bool]) -> None:
    """
    Test that the location of the installed TOML config file by
    `line_profiler.toml_config` works even when `importlib.resources`
    has been replaced by `importlib_resources` during runtime (see
    GitHub issue #405).
    """
    run_python = partial(run_python_in_venv, tmpdir=tmp_path, venv=venv)
    run_pip = partial(run_pip_in_venv, tmpdir=tmp_path, venv=venv)

    # Install the required `importlib_resources` version
    if version:
        ir = 'importlib_resources'
        if isinstance(version, str):
            ir = f'{ir} {version}'

        run_pip('install', ['--upgrade', ir]).check_returncode()
    run_pip('list')

    # Run python code which substitutes `importlib.resources` with
    # `importlib_resources` before importing `line_profiler` and see
    # what happens
    python_script = tmp_path / 'script.py'
    if version:
        preamble = textwrap.dedent("""
    import importlib
    import importlib_resources as _ir
    import sys

    importlib.resources = sys.modules['importlib.resources'] = _ir
    del _ir
        """)
    else:
        preamble = ''
    sanity_check = textwrap.dedent("""
    from line_profiler.toml_config import ConfigSource
    print(ConfigSource.from_default())
    """)
    write_text(python_script, f'{preamble}\n{sanity_check}')
    proc = run_python(['-W', 'always::DeprecationWarning', python_script])
    proc.check_returncode()
    assert not re.search('DeprecationWarning.*importlib[-_]?resources',
                         proc.stderr), proc.stderr


########################################################################
#            END: edge-case test for `importlib_resources`             #
########################################################################
