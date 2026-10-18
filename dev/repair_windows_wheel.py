"""Repair Windows wheels with the target-architecture MSVC runtime.

The x64 GitHub Actions runner can cross-compile ``win_arm64`` wheels, but its
PATH does not include the ARM64 Microsoft Visual C++ redistributable DLLs.
Delvewheel must search the ARM64 CRT directory, not the host x64 CRT directory.

Called by ``tool.cibuildwheel.windows.repair-wheel-command``.
"""

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
import subprocess
import sys


def _wheel_arch(wheel: Path) -> str:
    if wheel.name.endswith('-win_arm64.whl'):
        return 'arm64'
    if wheel.name.endswith('-win_amd64.whl'):
        return 'x64'
    if wheel.name.endswith('-win32.whl'):
        return 'x86'
    raise ValueError(f'Unrecognized Windows wheel platform tag: {wheel.name}')


def _redist_roots(environ: Mapping[str, str]) -> Iterator[Path]:
    """Find MSVC redist directories without assuming a VS edition/version."""
    redist_dir = environ.get('VCToolsRedistDir')
    if redist_dir:
        yield Path(redist_dir)

    vc_install = environ.get('VCINSTALLDIR')
    if vc_install:
        yield Path(vc_install) / 'Redist' / 'MSVC'

    for env_var in ('ProgramFiles', 'ProgramFiles(x86)'):
        program_files = environ.get(env_var)
        if program_files:
            visual_studio_dir = Path(program_files) / 'Microsoft Visual Studio'
            # Covers both VS 2022 (2022/Enterprise) and VS 2026 (18/Enterprise),
            # as well as other editions including BuildTools.
            yield from sorted(
                visual_studio_dir.glob('*/*/VC/Redist/MSVC'),
                reverse=True,
            )


def _find_crt_dir(arch: str, environ: Mapping[str, str]) -> Path | None:
    """Return an architecture-matching redistributable CRT directory."""
    visited: set[Path] = set()
    for redist_root in _redist_roots(environ):
        if redist_root in visited or not redist_root.is_dir():
            continue
        visited.add(redist_root)

        # VCToolsRedistDir often points at the version directory itself;
        # VCINSTALLDIR and Visual Studio paths point one directory higher.
        version_dirs = [redist_root]
        version_dirs.extend(
            sorted(
                (path for path in redist_root.iterdir() if path.is_dir()),
                reverse=True,
            )
        )
        for version_dir in version_dirs:
            for crt_dir in sorted(
                version_dir.glob(f'{arch}/Microsoft.VC*.CRT'), reverse=True
            ):
                if (crt_dir / 'msvcp140.dll').is_file():
                    return crt_dir
    return None


def repair_command(
    wheel: Path, dest_dir: Path, environ: Mapping[str, str]
) -> list[str]:
    arch = _wheel_arch(wheel)
    command = ['delvewheel', 'repair', '-w', str(dest_dir), '-v']
    if arch == 'arm64':
        # Do not search the native x64 system PATH for an ARM64 DLL.
        crt_dir = _find_crt_dir(arch, environ)
        if crt_dir is None:
            raise RuntimeError(
                'Cannot locate the ARM64 MSVC redistributable DLLs. '
                'Install the Visual Studio ARM64 C++ build tools / CRT '
                'components, or set VCToolsRedistDir to the installed '
                'MSVC Redist version directory.'
            )
        command.extend(['--add-path', str(crt_dir)])
    command.append(str(wheel))
    return command


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print('Usage: repair_windows_wheel.py WHEEL DEST_DIR', file=sys.stderr)
        return 2
    wheel, dest_dir = map(Path, args)
    command = repair_command(wheel, dest_dir, os.environ)
    print(f'[wheel repair] {command}', flush=True)
    subprocess.run(command, check=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
