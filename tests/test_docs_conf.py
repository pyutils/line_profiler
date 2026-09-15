"""Tests for helpers defined in the Sphinx configuration."""
import ast
from os.path import dirname, exists, join

import pytest

CONF_FPATH = join(dirname(dirname(__file__)), 'docs', 'source', 'conf.py')


def _load_parse_version():
    """Extract ``parse_version`` from ``conf.py`` without importing sphinx."""
    if not exists(CONF_FPATH):
        pytest.skip('docs/source/conf.py is not available')
    tree = ast.parse(open(CONF_FPATH, 'r').read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == 'parse_version':
            ns = {'exists': exists, 'join': join, 'dirname': dirname}
            exec(compile(ast.Module([node], []), CONF_FPATH, 'exec'), ns)
            return ns['parse_version']
    pytest.skip('conf.py does not define parse_version')


def test_parse_version(tmp_path):
    """``parse_version`` works on Python 3.14, where ``ast.Constant.s`` is gone.

    See https://github.com/pyutils/line_profiler/issues/429
    """
    parse_version = _load_parse_version()
    fpath = tmp_path / 'mod.py'
    fpath.write_text("__version__ = '1.2.3'\n")
    assert parse_version(str(fpath)) == '1.2.3'
