"""
Miscellaneous utilities that :py:mod:`line_profiler` uses.
"""

from __future__ import annotations

import enum
import os
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from functools import partial
from pathlib import Path
from reprlib import Repr
from tempfile import mkstemp
from textwrap import indent
from types import MethodType
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar
from typing_extensions import Self, Unpack


__all__ = ('StringEnum', 'CallbackRepr', 'block_indent', 'make_tempfile')

# Note: `typing.AnyStr` deprecated since 3.13
AnyStr = TypeVar('AnyStr', str, bytes)


class _StrEnumBase(str, enum.Enum):
    """
    Base class mimicking :py:class:`enum.StrEnum` in Python 3.11+.

    Example
    -------
    >>> import enum
    >>>
    >>>
    >>> class MyEnum(_StrEnumBase):
    ...     foo = enum.auto()
    ...     BAR = enum.auto()
    ...
    >>>
    >>> MyEnum.foo
    <MyEnum.foo: 'foo'>
    >>> MyEnum('bar')
    <MyEnum.BAR: 'bar'>
    >>> MyEnum('baz')
    Traceback (most recent call last):
      ...
    ValueError: 'baz' is not a valid MyEnum
    """

    @staticmethod
    def _generate_next_value_(name: str, *_, **__) -> str:
        return name.lower()

    def __eq__(self, other: object) -> bool:
        return self.value == other

    def __str__(self) -> str:
        return self.value


try:
    from enum import StrEnum as _StrEnum
except ImportError:
    if not TYPE_CHECKING:  # Don't confuse the typechecker
        _StrEnum = _StrEnumBase


class StringEnum(_StrEnum):
    """
    Convenience wrapper around :py:class:`enum.StrEnum`.

    Example
    -------
    >>> import enum
    >>>
    >>>
    >>> class MyEnum(StringEnum):
    ...     foo = enum.auto()
    ...     BAR = enum.auto()
    ...
    >>>
    >>> MyEnum.foo
    <MyEnum.foo: 'foo'>
    >>> MyEnum('bar')
    <MyEnum.BAR: 'bar'>
    >>> bar = MyEnum('BAR')  # Case-insensitive
    >>> bar
    <MyEnum.BAR: 'bar'>
    >>> assert isinstance(bar, str)
    >>> assert bar == 'bar'
    >>> str(bar)
    'bar'
    """

    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        if not isinstance(value, str):
            return None
        members = {
            name.casefold(): instance
            for name, instance in cls.__members__.items()
        }
        return members.get(value.casefold())


class _ReprAttributes(TypedDict, total=False):
    """
    Note:
        We use this typed dict instead of directly supplying them in the
        :py:meth:`CallbackRepr.__init__()` signature, because we don't
        want to bother with the default values there.
    """
    maxlevel: int
    maxtuple: int
    maxlist: int
    maxarray: int
    maxdict: int
    maxset: int
    maxfrozenset: int
    maxdeque: int
    maxstring: int
    maxlog: int
    maxother: int
    fillvalue: str
    indent: str | int | None


class CallbackRepr(Repr):
    """
    :py:class:`reprlib.Repr` subclass to help with representing cleanup
    callbacks, special-casing certain relevant object types (see
    examples below).

    Example:
        >>> from functools import partial
        >>> from sys import version_info

        >>> class MyEnviron(dict):
        ...     def some_method(self) -> None:
        ...         ...
        ...
        >>>
        >>> class MyRepr(CallbackRepr):
        ...     # Since we can't instantiate a new `os._Environ`, test
        ...     # the relevant method with a mock
        ...     repr_MyEnviron = CallbackRepr.repr__Environ
        ...
        >>>
        >>> r = MyRepr(maxenv=3, maxargs=4, maxstring=15)

        Environ-dict formatting:

        >>> my_env = MyEnviron(
        ...     foo='1',
        ...     bar='2',
        ...     this_varname_is_long_but_isnt_truncated=(
        ...         "THIS VALUE IS TRUNCATED BECAUSE IT'S TOO LONG"
        ...     ),
        ...     baz='4',
        ... )
        >>> print(r.repr(my_env))
        environ({'foo': '1', 'bar': '2', \
'this_varname_is_long_but_isnt_truncated': 'THIS ... LONG', ...})

        Partial-object formatting:

        >>> r.maxenv = 0
        >>> print(r.repr(my_env.some_method))
        <bound method MyEnviron.some_method of environ({...})>

        Bound-method formatting:

        >>> r.maxargs = 0
        >>> callback_1 = partial(int, base=8)
        >>> print(r.repr(callback_1))
        functools.partial(<class 'int'>, ...)

        Indentation (Python 3.12+):

        >>> if version_info < (3, 12):
        ...     from pytest import skip
        ...
        ...     skip(
        ...         '`Repr.indent` not available on {}.{},{}'
        ...         .format(*sys.version_info)
        ...     )

        >>> r = MyRepr(maxenv=2, maxargs=4)
        >>> r.indent = 2
        >>> callback_1 = partial(int, base=8)
        >>> print(r.repr(callback_1))
        functools.partial(
          <class 'int'>,
          base=8,
        )

        >>> callback_2 = partial(min, 5, 4, 3, 2, 1)
        >>> r.indent = '----'
        >>> print(r.repr(callback_2))
        functools.partial(
        ----<built-in function min>,
        ----5,
        ----4,
        ----3,
        ----2,
        ----...,
        )

        >>> r.indent = '    '
        >>> r.maxenv = 2
        >>> print(r.repr(my_env.some_method))
        <bound method MyEnviron.some_method of environ({
                                                   'foo': '1',
                                                   'bar': '2',
                                                   ...,
                                               })>
    """
    def __init__(
        self,
        *,
        maxargs: int = 5,
        maxenv: int = 3,
        **kwargs: Unpack[_ReprAttributes]
    ) -> None:
        super().__init__()  # kwargs are 3.12+
        valid_kwargs = (
            _ReprAttributes.__optional_keys__
            | _ReprAttributes.__required_keys__
        )
        for k, v in kwargs.items():
            if k in valid_kwargs:
                setattr(self, k, v)
        self.maxargs = maxargs
        self.maxenv = maxenv

    def repr__Environ(self, env: os._Environ[AnyStr], level: int) -> str:
        """
        Format :py:data:`os.environ` or :py:data:`os.environb`.
        """
        get: Callable[[AnyStr], str] = partial(self.repr1, level=level-1)
        # Truncate envvar values, but not their names
        envvars = ['{!r}: {}'.format(k, get(v)) for k, v in env.items()]
        return self._format_items(envvars, ('environ({', '})'), self.maxenv)

    def repr_method(self, method: MethodType, level: int) -> str:
        """
        Format a :py:class:`types.MethodType`.
        """
        instance = self.repr1(method.__self__, level-1)
        func = getattr(method.__func__, '__qualname__', '?')
        prefix, suffix = f'<bound method {func} of ', '>'
        # Take care of possible multi-line reprs
        return block_indent(instance, prefix) + suffix

    def repr_partial(self, ptl: partial, level: int) -> str:
        """
        Format a :py:func:`functools.partial`.
        """
        name = '{0.__module__}.{0.__qualname__}'.format(type(ptl))
        # The +1 is to account for `ptl.func`
        return self._format_call(
            level, (name + '(', ')'), self.maxargs + 1,
            [ptl.func, *ptl.args], ptl.keywords,
        )

    def format_call(self, /, *args, **kwargs) -> str:
        """
        Convenience method for Formating a call a la
        :py:meth:`inspect.BoundArguments.__str__`.

        Example:
            >>> r = CallbackRepr(maxargs=3, maxlist=3)
            >>> print(r.format_call(
            ...     [1, 2, 3, 4, 5], 'foo', spam=1, ham=2,
            ... ))
            ([1, 2, 3, ...], 'foo', spam=1, ...)
        """
        return self._format_call(
            self.maxlevel, ('(', ')'), self.maxargs, args, kwargs,
        )

    def _format_call(
        self,
        level: int,
        delims: tuple[str, str],
        maxargs: int,
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
    ) -> str:
        get: Callable[[Any], str] = partial(self.repr1, level=level-1)
        args = [get(arg) for arg in args]
        args.extend('{}={}'.format(k, get(v)) for k, v in kwargs.items())
        return self._format_items(args, delims, maxargs)

    def _format_items(
        self,
        items: Collection[str],
        delims: tuple[str, str],
        maxlen: int | None = None,
    ) -> str:
        start, end = delims
        if maxlen is not None and len(items) > maxlen:
            items = list(items)[:maxlen] + ['...']
        indent_prefix: str | None = self._get_indent()
        if indent_prefix is None or not items:
            return '{}{}{}'.format(start, ', '.join(items), end)
        return '\n'.join([
            start, *(indent(item + ',', indent_prefix) for item in items), end,
        ])

    if sys.version_info >= (3, 12):
        # Note: `.indent` only available since 3.12
        def _get_indent(self) -> str | None:
            indent = self.indent
            if indent is None or isinstance(indent, str):
                return indent
            return ' ' * indent
    else:
        @staticmethod
        def _get_indent() -> None:
            return None


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
