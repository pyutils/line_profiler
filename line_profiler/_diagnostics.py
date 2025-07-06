"""
Global state initialized at import time.
Used for hidden arguments and developer features.
"""
from line_profiler import _logger
import os


def _boolean_environ(key):
    """
    Args:
        key (str)

    Returns:
        bool
    """
    value = os.environ.get(key, '').lower()
    TRUTHY_ENVIRONS = {'true', 'on', 'yes', '1'}
    return value in TRUTHY_ENVIRONS


DEBUG = _boolean_environ('LINE_PROFILER_DEBUG')
NO_EXEC = _boolean_environ('LINE_PROFILER_NO_EXEC')
KEEP_TEMPDIRS = _boolean_environ('LINE_PROFILER_KEEP_TEMPDIRS')
STATIC_ANALYSIS = _boolean_environ('LINE_PROFILER_STATIC_ANALYSIS')

log = _logger.Logger('line_profiler', backend='auto')
