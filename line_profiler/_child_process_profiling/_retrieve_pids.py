"""
Retrieve active PIDs; uses platform-specific solutions.
"""
import csv
import subprocess
import sys
from collections.abc import Callable, Collection
from shutil import which
from textwrap import indent
from typing import NoReturn


__all__ = (
    'CAN_RETRIEVE_PIDS', 'processes_are_alive', 'get_active_processes',
)

CAN_RETRIEVE_PIDS: bool
get_active_processes: Callable[[], set[int]]


def _find_executable(name: str) -> str:
    maybe_path = which(name)
    if maybe_path:
        return maybe_path
    raise RuntimeError(f'cannot find required executable for `{name}`')


def processes_are_alive(pids: Collection[int]) -> dict[int, bool]:
    """
    Returns:
        procs_alive (dict[int, bool]):
            Dictionary mapping each PID to whether it is alive
    """
    all_pids = get_active_processes()
    return {p: p in all_pids for p in set(pids)}


def get_active_processes_posix() -> set[int]:
    """
    Get the active PIDs using ``ps`` in POSIX(-like) environments.
    """
    # `a`: processes from all users
    # `u`: excluded, because we don't want usernames clogging up the 1st
    #      column
    # `x`: include detached processes
    cmd = [_find_executable('ps'), 'ax', '-o', 'pid']
    ps = subprocess.run(cmd, capture_output=True, text=True, check=True)
    _, *procs = ps.stdout.splitlines()  # Drop the header line
    return {int(p.strip()) for p in procs}


def get_active_processes_windows() -> set[int]:
    """
    Get the active PIDs using ``tasklist`` on Windows.
    """
    # CSV formatted output
    cmd = [_find_executable('tasklist'), '/fo', 'csv']
    tasklist = subprocess.run(cmd, capture_output=True, text=True, check=True)
    rows = tasklist.stdout.lower().splitlines()  # Normalize cases
    # XXX: this is *probably* locale-safe given that even JP has PID as
    # a header... but who knows.
    # Might have to test in an RTL language...
    try:
        return {int(row['pid']) for row in csv.DictReader(rows)}
    except Exception as e:  # nocover
        n = 10
        head_raw = '\n'.join(tasklist.stdout.splitlines()[:n])
        error = type(e).__name__
        if str(e):
            error = f'{error}: {e}'
        raise RuntimeError(  # Context for debugging
            'Error reading PIDs from `tasklist` output '
            f'({error}; first {n} line(s)):\n{indent(head_raw, "  ")}'
        ) from e


def get_active_processes_not_supported() -> NoReturn:  # nocover
    """
    Unsupported platform; calling this results in a
    :py:class:`NotImplementedError`.
    """
    raise NotImplementedError(f'unsupported platform: `{sys.platform}`')


if sys.platform.startswith('win32'):
    CAN_RETRIEVE_PIDS = True
    get_active_processes = get_active_processes_windows
elif sys.platform.startswith('wasi'):  # nocover
    CAN_RETRIEVE_PIDS = False
    get_active_processes = get_active_processes_not_supported
else:
    CAN_RETRIEVE_PIDS = True
    get_active_processes = get_active_processes_posix
