"""Tests for helpers defined in the Sphinx configuration."""
from __future__ import annotations

import ast
import os.path
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


CONF_FPATH = Path(__file__).parent.parent / 'docs' / 'source' / 'conf.py'


@pytest.fixture(scope='module')
def parse_version() -> Callable[[str | os.PathLike[str]], Any]:
    """
    Extract ``parse_version`` from ``conf.py`` without importing sphinx
    """
    if not CONF_FPATH.exists():
        pytest.skip('docs/source/conf.py is not available')
    tree = ast.parse(CONF_FPATH.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == 'parse_version':
            ns = dict(vars(os.path))
            exec(compile(ast.Module([node], []), CONF_FPATH, 'exec'), ns)
            return ns['parse_version']
    pytest.skip('conf.py does not define `parse_version()`')


def test_parse_version(
    parse_version: Callable[[str | os.PathLike[str]], Any], tmp_path: Path,
) -> None:
    """
    Test that ``parse_version()`` works on Python 3.14, where
    :py:attr:`ast.Constant.s` is gone.

    See https://github.com/pyutils/line_profiler/issues/429
    """
    fpath = tmp_path / 'mod.py'
    fpath.write_text("__version__ = '1.2.3'\n")
    assert parse_version(fpath) == '1.2.3'
