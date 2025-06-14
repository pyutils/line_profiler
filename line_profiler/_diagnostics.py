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

# DEBUG_TEMPDIR = DEBUG or _boolean_environ('LINE_PROFILER_DEBUG_TEMPDIR')
# DEBUG_CORE = DEBUG or _boolean_environ('XDOCTEST_DEBUG_CORE')
# DEBUG_RUNNER = DEBUG or _boolean_environ('XDOCTEST_DEBUG_RUNNER')
# DEBUG_DOCTEST = DEBUG or _boolean_environ('XDOCTEST_DEBUG_DOCTEST')
log = _logger.Logger()
