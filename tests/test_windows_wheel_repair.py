"""Offline unit tests for the Windows wheel-repair helper."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'dev/repair_windows_wheel.py'
spec = spec_from_file_location('repair_windows_wheel', SCRIPT)
assert spec is not None and spec.loader is not None
repair = module_from_spec(spec)
spec.loader.exec_module(repair)


def _make_crt(root: Path, arch: str) -> Path:
    crt = root / arch / 'Microsoft.VC143.CRT'
    crt.mkdir(parents=True)
    (crt / 'msvcp140.dll').touch()
    return crt


def test_arm64_uses_arm64_msvc_crt(tmp_path):
    redist = tmp_path / 'Redist' / 'MSVC' / '14.44.35208'
    _make_crt(redist, 'x64')
    arm_crt = _make_crt(redist, 'arm64')
    wheel = Path('line_profiler-5.0.2-cp311-cp311-win_arm64.whl')
    dest = tmp_path / 'wheelhouse'
    result = repair.repair_command(wheel, dest, {'VCToolsRedistDir': str(redist)})
    assert result == [
        'delvewheel', 'repair', '-w', str(dest), '-v',
        '--add-path', str(arm_crt), str(wheel),
    ]


def test_arm64_discovers_visual_studio_installation(tmp_path):
    # Simulates the VS 2026 layout on windows-latest, without depending on
    # which VS edition or MSVC runtime version happens to be installed.
    root = tmp_path / 'Microsoft Visual Studio/18/Enterprise/VC/Redist/MSVC'
    arm_crt = _make_crt(root / '14.50.10001', 'arm64')
    found = repair._find_crt_dir('arm64', {'ProgramFiles': str(tmp_path)})
    assert found == arm_crt


def test_arm64_discovers_buildtools_installation(tmp_path):
    root = (
        tmp_path / 'Microsoft Visual Studio/2022/BuildTools/VC/Redist/MSVC'
    )
    arm_crt = _make_crt(root / '14.44.35208', 'arm64')
    found = repair._find_crt_dir('arm64', {'ProgramFiles(x86)': str(tmp_path)})
    assert found == arm_crt


def test_arm64_missing_runtime_fails_instead_of_using_host_crt(tmp_path):
    redist = tmp_path / 'Redist' / 'MSVC' / '14.44.35208'
    _make_crt(redist, 'x64')
    with pytest.raises(RuntimeError, match='ARM64 MSVC redistributable'):
        repair.repair_command(
            Path('line_profiler-5.0.2-cp315-cp315-win_arm64.whl'),
            tmp_path / 'out',
            {'VCToolsRedistDir': str(redist)},
        )


def test_amd64_keeps_default_delvewheel_repair(tmp_path):
    wheel = Path('line_profiler-5.0.2-cp315-cp315-win_amd64.whl')
    result = repair.repair_command(wheel, tmp_path, {})
    assert result == ['delvewheel', 'repair', '-w', str(tmp_path), '-v', str(wheel)]


def test_delvewheel_invocation_uses_subprocess_arguments(tmp_path, monkeypatch):
    wheel = Path('line_profiler-5.0.2-cp315-cp315-win_amd64.whl')
    expected = repair.repair_command(wheel, tmp_path, {})
    seen = []

    def fake_run(command, *, check):
        seen.append((command, check))

    monkeypatch.setattr(repair.subprocess, 'run', fake_run)
    monkeypatch.setattr(repair.os, 'environ', {})
    assert repair.main([str(wheel), str(tmp_path)]) == 0
    assert seen == [(expected, True)]


def test_unknown_platform_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='Unrecognized Windows wheel'):
        repair.repair_command(Path('line_profiler-5.0.2-py3-none-any.whl'), tmp_path, {})
