"""
Tests for :py:mod:`line_profiler.autoprofile.eager_preimports`.

Notes
-----
Most of the features are already covered by the doctests.
"""
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from operator import methodcaller
from runpy import run_path
from tempfile import TemporaryDirectory
from textwrap import dedent
from types import SimpleNamespace
from typing import Collection, Generator, Sequence, Type, Optional, Union
from uuid import uuid4
from warnings import catch_warnings

import pytest
try:
    import flake8  # noqa: F401
except ImportError:
    HAS_FLAKE8 = False
else:
    HAS_FLAKE8 = True

from line_profiler.autoprofile.eager_preimports import (
    write_eager_import_module as write_module)


def write(path: Path, content: Optional[str] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is None:
        path.touch()
    else:
        path.write_text(dedent(content).strip('\n'))


@pytest.fixture
def sample_module(tmp_path: Path) -> str:
    """
    Write a module and put it in :py:data:`sys.path`.  When we're done,
    reset :py:data:`sys.path` and `sys.modules`.
    """
    def gen_names() -> Generator[str, None, None]:
        while True:
            yield 'my_module_' + str(uuid4()).replace('-', '_')

    old_path = sys.path.copy()
    old_modules = sys.modules.copy()
    module_name = next(name for name in gen_names() if name not in old_modules)
    new_path = tmp_path / '_modules'
    write(new_path / module_name / '__init__.py')
    write(new_path / module_name / 'foo' / '__init__.py')
    write(new_path / module_name / 'foo' / 'bar.py')
    write(new_path / module_name / 'foo' / 'baz.py',
          """
          '''
          This is a bad module.
          '''
          raise AssertionError
          """)
    write(new_path / module_name / 'foobar.py')
    try:
        sys.path.insert(0, str(new_path))
        yield module_name
    finally:
        sys.path.clear()
        sys.path[:] = old_path
        assert str(new_path) not in sys.path
        sys.modules.clear()
        sys.modules.update(old_modules)
        assert module_name not in sys.modules


@pytest.mark.parametrize(
    ('adder', 'xc'),
    [('foo; bar', ValueError), (1, TypeError), ('(foo\n .bar)', ValueError)])
def test_write_eager_import_module_wrong_adder(
        adder: str, xc: Type[Exception]) -> None:
    """
    Test passing an erroneous ``adder`` to
    :py:meth:`~.write_eager_import_module()`.
    """
    with pytest.raises(xc):
        write_module(['foo'], adder=adder)


@pytest.mark.skipif(not HAS_FLAKE8, reason='no `flake8`')
def test_written_module_pep8_compliance(sample_module: str):
    """
    Test that the module written by
    :py:meth:`~.write_eager_import_module()` passes linting by
    :py:mod:`flake8`.
    """
    with TemporaryDirectory() as tmpdir:
        module = Path(tmpdir) / 'module.py'
        with module.open(mode='w') as fobj:
            write_module([sample_module + '.foobar'],
                         recurse=[sample_module + '.foo'], stream=fobj)
        print(module.read_text())
        (subprocess
         .run([sys.executable, '-m', 'flake8',
               '--extend-ignore=E501',  # Allow long lines
               module])
         .check_returncode())


@pytest.mark.parametrize(
    ('dotted_paths', 'recurse', 'warnings', 'error'),
    [(['__MODULE__.foobar'], ['__MODULE__.foo'],
      # `foo.baz` is indirectly included, so its raising an error
      # shouldn't cause the script to error out
      [{'target cannot', '__MODULE__.foo.baz'}],
      None),
     # We don't recurse down `__MODULE__.foo`, so that doesn't give a
     # warning; but `__MODULE__.baz` cannot be imported because it
     # doesn't exist
     (['__MODULE__.foo', '__MODULE__.baz'], False,
      [{'target cannot', '__MODULE__.baz'}], None),
     # If we do recurse however, `__MODULE__.foo.baz` also ends up in
     # the warning
     # (also there's a `__MODULE___foo` which doesn't exist, about which
     # the warning is issued during module generation)
     (['__MODULE__' + '_foo', '__MODULE__', '__MODULE__.baz'], True,
      [{'target cannot', '__MODULE__' + '_foo'},  # Fails at write
       {'targets cannot',  # Fails at import
        '__MODULE__.foo.baz', '__MODULE__.baz'}],
      None),
     # And if the problematic module is an explicit target, raise the
     # error
     (['__MODULE__', '__MODULE__.foo.baz'], False, [], AssertionError)])
def test_written_module_error_handling(
        sample_module: str,
        dotted_paths: Collection[str],
        recurse: Union[Collection[str], bool],
        warnings: Sequence[Collection[str]],
        error: Union[Type[Exception], None]):
    """
    Test that the module written by
    :py:meth:`~.write_eager_import_module()` gracefully handles errors
    for implicitly included modules.
    """
    replace = methodcaller('replace', '__MODULE__', sample_module)
    dotted_paths = [replace(target) for target in dotted_paths]
    if recurse not in (True, False):
        recurse = [replace(target) for target in recurse]
    warnings = [{replace(fragment) for fragment in fragments}
                for fragments in warnings]
    with TemporaryDirectory() as tmpdir:
        module = Path(tmpdir) / 'module.py'
        with ExitStack() as stack:
            enter = stack.enter_context
            # Set up the warning capturing early so that we catch both
            # warnings at module-generation time and execution time
            captured_warnings = enter(catch_warnings(record=True))
            with module.open(mode='w') as fobj:
                write_module(dotted_paths, recurse=recurse, stream=fobj)
            print(module.read_text())
            if error is not None:
                enter(pytest.raises(error))
            # Just use a dummy object, no need to instantiate a profiler
            prof = SimpleNamespace(
                add_imported_function_or_module=lambda *_, **__: 0)
            run_path(str(module), {'profile': prof}, 'module')
    assert len(captured_warnings) == len(warnings)
    for warning, fragments in zip(captured_warnings, warnings):
        for fragment in fragments:
            assert fragment in str(warning.message)
