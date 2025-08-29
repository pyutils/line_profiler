"""
Tests for profiling Cython code.
"""
import math
import os
import subprocess
import sys
from importlib import reload, import_module
from importlib.util import find_spec
from io import StringIO
from operator import methodcaller
from pathlib import Path
from types import ModuleType
from typing import Generator, Tuple
from uuid import uuid4

import pytest

from line_profiler._line_profiler import (
    CANNOT_LINE_TRACE_CYTHON, find_cython_source_file)
from line_profiler.line_profiler import (  # type:ignore[attr-defined]
    get_code_block, LineProfiler)


def propose_name(prefix: str) -> Generator[str, None, None]:
    while True:
        yield '_'.join([prefix, *str(uuid4()).split('-')])


def _install_cython_example(
        tmp_path_factory: pytest.TempPathFactory,
        editable: bool) -> Generator[Tuple[Path, str], None, None]:
    """
    Install the example Cython module in a name-clash-free manner.
    """
    source = Path(__file__).parent / 'cython_example'
    assert source.is_dir()
    module = next(name for name in propose_name('cython_example')
                  if not find_spec(name))
    replace = methodcaller('replace', 'cython_example', module)
    pip = [sys.executable, '-m', 'pip']
    tmp_path = tmp_path_factory.mktemp('cython_example')
    # Replace all references to `cython_example` with the actual module
    # name and write to the tempdir
    for prefix, _, files in os.walk(source):
        dir_in = Path(prefix)
        for fname in files:
            _, ext = os.path.splitext(fname)
            if ext not in {'.py', '.pyx', '.pxd', '.toml'}:
                continue
            dir_out = tmp_path.joinpath(*(
                replace(part) for part in dir_in.relative_to(source).parts))
            dir_out.mkdir(exist_ok=True)
            file_in = dir_in / fname
            file_out = dir_out / replace(fname)
            file_out.write_text(replace(file_in.read_text()))
    # There should only be one Cython source file
    cython_source, = tmp_path.glob('*.pyx')
    pip_install = pip + ['install', '--verbose']
    if editable:
        pip_install += ['--editable', str(tmp_path)]
    else:
        pip_install.append(str(tmp_path))
    try:
        subprocess.run(pip_install).check_returncode()
        subprocess.run(pip + ['list']).check_returncode()
        yield cython_source, module
    finally:
        pip_uninstall = pip + ['uninstall', '--verbose', '--yes', module]
        subprocess.run(pip_uninstall).check_returncode()


@pytest.fixture(scope='module')
def cython_example(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[Tuple[Path, ModuleType], None, None]:
    """
    Install the example Cython module, yield the path to the Cython
    source file and the corresponding module, uninstall it at teardown.
    """
    # With editable installs, we need to refresh `sys.meta_path` before
    # the installed module is available
    for path, mod_name in _install_cython_example(tmp_path_factory, True):
        reload(import_module('site'))
        yield (path, import_module(mod_name))


def test_recover_cython_source(cython_example: Tuple[Path, ModuleType]) -> None:
    """
    Check that Cython sources are correctly located by
    `line_profiler._line_profiler.find_cython_source_file()` and
    `line_profiler.line_profiler.get_code_block()`.
    """
    expected_source, module = cython_example
    for func in module.cos, module.sin:
        source = find_cython_source_file(func)
        assert source
        assert expected_source.samefile(source)
        source_lines = get_code_block(source, func.__code__.co_firstlineno)
        for line, prefix in [(source_lines[0], '# Start: '),
                             (source_lines[-1], '# End: ')]:
            assert line.rstrip('\n').endswith(prefix + func.__name__)


@pytest.mark.skipif(
    CANNOT_LINE_TRACE_CYTHON,
    reason='Cannot line-trace Cython code in version '
    + '.'.join(str(v) for v in sys.version_info[:3]))
def test_profile_cython_source(cython_example: Tuple[Path, ModuleType]) -> None:
    """
    Check that calls to Cython functions (built with the appropriate
    compile-time options) can be profiled.
    """
    prof_cos = LineProfiler()
    prof_sin = LineProfiler()

    _, module = cython_example
    cos = prof_cos(module.cos)
    sin = prof_sin(module.sin)
    assert pytest.approx(cos(.125, 10)) == math.cos(.125)
    assert pytest.approx(sin(2.5, 3)) == 2.5 - 2.5 ** 3 / 6 + 2.5 ** 5 / 120

    for prof, func, expected_nhits in [
            (prof_cos, 'cos', 10), (prof_sin, 'sin', 3)]:
        with StringIO() as fobj:
            prof.print_stats(fobj)
            result = fobj.getvalue()
            print(result)
        assert ('Function: ' + func) in result
        nhits = 0
        for line in result.splitlines():
            if all(chunk in line for chunk in ('result', '+', 'last_term')):
                nhits += int(line.split()[1])
        assert nhits == expected_nhits
