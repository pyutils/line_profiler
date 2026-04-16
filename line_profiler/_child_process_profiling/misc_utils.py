"""
Misc. utility functions used by the subpackage.
"""
import os
from pathlib import Path
from tempfile import mkstemp
from textwrap import indent


__all__ = ('block_indent', 'make_tempfile')


def block_indent(string: str, prefix: str, fill_char: str = ' ') -> str:
    r"""
    Example:
        >>> string = 'foo\nbar\nbaz'
        >>> print(string)
        foo
        bar
        baz
        >>> print(block_indent(string, '++++', '-'))
        ++++foo
        ----bar
        ----baz
    """
    width = len(prefix)
    return prefix + indent(string, fill_char * width)[width:]


def make_tempfile(**kwargs) -> Path:
    """
    Convenience wrapper around :py:func:`tempfile.mkstemp`, discarding
    and closing the integer handle (which if left unattended causes
    problems on some platforms).
    """
    handle, fname = mkstemp(**kwargs)
    try:
        return Path(fname)
    finally:
        os.close(handle)
