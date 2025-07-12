"""
Global state initialized at import time.
Used for hidden arguments and developer features.
"""
import os
import sys
from types import ModuleType
from line_profiler import _logger


def _boolean_environ(
        envvar,
        truey=frozenset({'1', 'on', 'true', 'yes'}),
        falsy=frozenset({'0', 'off', 'false', 'no'}),
        default=False):
    r"""
    Args:
        envvar (str)
            Name for the environment variable to read from.
        truey (Collection[str])
            Values to be considered truey.
        falsy (Collection[str])
            Values to be considered falsy.
        default (bool)
            Default boolean value to resolve to.

    Returns:
        :py:data:`True`
            If the (case-normalized) environment variable is equal to
            any of ``truey``.
        :py:data:`False`
            If the (case-normalized) environment variable is equal to
            any of ``falsy``.
        ``default``
            Otherwise.

    Example:
        >>> from os import environ
        >>> from subprocess import run
        >>> from sys import executable
        >>> from textwrap import dedent
        >>>
        >>>
        >>> def resolve_in_subproc(value, default,
        ...                        envvar='MY_ENVVAR',
        ...                        truey=('foo',), falsy=('bar',)):
        ...     code = dedent('''
        ...     from {0.__module__} import {0.__name__}
        ...     print({0.__name__}({1!r}, {2!r}, {3!r}, {4!r}))
        ...     ''').strip('\n').format(_boolean_environ, envvar,
        ...                             tuple(truey), tuple(falsy),
        ...                             bool(default))
        ...     env = environ.copy()
        ...     env[envvar] = value
        ...     proc = run([executable, '-c', code],
        ...                capture_output=True, env=env, text=True)
        ...     proc.check_returncode()
        ...     return {'True': True,
        ...             'False': False}[proc.stdout.strip()]
        ...
        >>>
        >>> # Truey value
        >>> assert resolve_in_subproc('FOO', True) == True
        >>> assert resolve_in_subproc('FOO', False) == True
        >>> # Falsy value
        >>> assert resolve_in_subproc('BaR', True) == False
        >>> assert resolve_in_subproc('BaR', False) == False
        >>> # Mismatch -> fall back to default
        >>> assert resolve_in_subproc('baz', True) == True
        >>> assert resolve_in_subproc('baz', False) == False
    """
    # (TODO: migrate to `line_profiler.cli_utils.boolean()` after
    # merging #335)
    try:
        value = os.environ.get(envvar).casefold()
    except AttributeError:  # None
        return default
    non_default_values = falsy if default else truey
    if value in {v.casefold() for v in non_default_values}:
        return not default
    return default


# `kernprof` switches
DEBUG = _boolean_environ('LINE_PROFILER_DEBUG')
NO_EXEC = _boolean_environ('LINE_PROFILER_NO_EXEC')
KEEP_TEMPDIRS = _boolean_environ('LINE_PROFILER_KEEP_TEMPDIRS')
STATIC_ANALYSIS = _boolean_environ('LINE_PROFILER_STATIC_ANALYSIS')

# `line_profiler._line_profiler` switches
WRAP_TRACE = _boolean_environ('LINE_PROFILER_WRAP_TRACE')
SET_FRAME_LOCAL_TRACE = _boolean_environ('LINE_PROFILER_SET_FRAME_LOCAL_TRACE')
_MUST_USE_LEGACY_TRACE = not isinstance(
    getattr(sys, 'monitoring', None), ModuleType)
USE_LEGACY_TRACE = (
    _MUST_USE_LEGACY_TRACE
    or _boolean_environ('LINE_PROFILER_CORE',
                        # Also provide `coverage-style` aliases
                        truey={'old', 'legacy', 'ctrace'},
                        falsy={'new', 'sys.monitoring', 'sysmon'},
                        default=_MUST_USE_LEGACY_TRACE))

log = _logger.Logger('line_profiler', backend='auto')
