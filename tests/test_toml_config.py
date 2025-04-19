"""
Test the handling of TOML configs.
"""
import os
import pathlib
import pytest
import textwrap
from line_profiler.toml_config import get_config, get_default_config


def write_text(path: pathlib.Path, text: str, /, *args, **kwargs) -> int:
    text = textwrap.dedent(text).strip('\n')
    return path.write_text(text, *args, **kwargs)


@pytest.fixture(autouse=True)
def fresh_curdir(monkeypatch: pytest.MonkeyPatch,
                tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """
    Ensure that the tests start on a clean slate: they shouldn't see
    the environment variable `${LINE_PROFILER_RC}`, nor should the
    `get_config()` lookup find any config file.

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
    `fresh_curdir()` fixture.
    """
    assert 'LINE_PROFILER_RC' not in os.environ
    assert get_config() == get_default_config()


def test_default_config_deep_copy() -> None:
    """
    Test that `line_profiler.toml_config.get_default_config()` always
    return a fresh copy of the default config.
    """
    (default_1, _), (default_2, _) = (get_default_config() for _ in (1, 2))
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
    default_config, default_source = get_default_config()
    toml = fresh_curdir / 'foo.toml'
    write_text(toml, """
    [unrelated.table]
    foo = 'foo'  # This should be ignored

    [tool.line_profiler.write]
    output_prefix = 'my_prefix'  # This is parsed and retained
    nonexistent_key = 'nonexistent_value'  # This should be ignored
    """)
    loaded_config, loaded_source = get_config(toml)
    assert loaded_source.samefile(toml)
    assert loaded_config['write']['output_prefix'] == 'my_prefix'
    assert 'nonexistent_key' not in loaded_config['write']
    del loaded_config['write']['output_prefix']
    del default_config['write']['output_prefix']
    assert loaded_config == default_config


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
        get_config(toml)


def test_config_lookup_hierarchy(monkeypatch: pytest.MonkeyPatch,
                                 fresh_curdir: pathlib.Path) -> None:
    """
    Test the hierarchy according to which we load config files.
    """
    _, default = get_default_config()
    # Lowest priority: `pyproject.toml` or `line_profiler_rc.toml` in
    # an ancestral directory
    curdir = fresh_curdir
    lowest_priority = curdir / 'line_profiler_rc.toml'
    lowest_priority.touch()
    curdir = curdir / 'child'
    curdir.mkdir()
    monkeypatch.chdir(curdir)
    assert get_config()[1].samefile(lowest_priority)
    # Higher priority: the same in the current directory
    # (`line_profiler_rc.toml` preferred over `pyproject.toml`)
    lower_priority = curdir / 'pyproject.toml'
    lower_priority.touch()
    assert get_config()[1].samefile(lower_priority)
    low_priority = curdir / 'line_profiler_rc.toml'
    low_priority.touch()
    assert get_config()[1].samefile(low_priority)
    # Higher priority: a file specified by the `${LINE_PROFILER_RC}`
    # environment variable (but we fall back to the default if that
    # fails)
    monkeypatch.setenv('LINE_PROFILER_RC', 'foo.toml')
    assert get_config()[1].samefile(default)
    high_priority = curdir / 'foo.toml'
    high_priority.touch()
    assert get_config()[1].samefile(high_priority)
    # Highest priority: a file passed explicitly to the `config`
    # parameter
    highest_priority = curdir.parent / 'bar.toml'
    with pytest.raises(FileNotFoundError):
        get_config(highest_priority)
    highest_priority.touch()
    assert get_config(highest_priority)[1].samefile(highest_priority)
