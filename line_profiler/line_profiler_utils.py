"""
Miscellaneous utilities that :py:mod:`line_profiler` uses.
"""

from __future__ import annotations

import enum
import os
import sys
from collections.abc import (
    Callable, Collection, Mapping, MutableMapping, MutableSequence, Sequence,
)
from functools import partial, wraps
from operator import methodcaller
from pathlib import Path
from reprlib import Repr
from tempfile import mkstemp
from textwrap import indent
from types import MethodType
from typing import TYPE_CHECKING, Any, Generic, TypedDict, TypeVar, final
from typing_extensions import Self, ParamSpec, Unpack


__all__ = (
    'StringEnum', 'restore', 'CallbackRepr', 'block_indent', 'make_tempfile',
)

# Note: `typing.AnyStr` deprecated since 3.13
AnyStr = TypeVar('AnyStr', str, bytes)
T = TypeVar('T')
K = TypeVar('K')
V = TypeVar('V')
T1 = TypeVar('T1')
T2 = TypeVar('T2')
PS = ParamSpec('PS')


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


@final
class restore(Generic[T1, T2]):
    """
    Context manager for restoring a collection like :py:data:`sys.path`
    after running code which potentially modifies it.

    Notes:
        - Mainly to be used via the class-method instantiators.

        - Also permits being used as a decorator.

        - Instances are reentrant (see e.g. the doctest of
          :py:meth:`.sequence`).
    """
    def __init__(
        self,
        obj: T1,
        getter: Callable[[T1], T2],
        setter: Callable[[T1, T2], Any],
    ) -> None:
        self.obj = obj
        self._setter = setter
        self._getter = getter
        self._stack: list[T2] = []

    def __enter__(self) -> None:
        self._stack.append(self._getter(self.obj))

    def __exit__(self, *_, **__) -> None:
        self._setter(self.obj, self._stack.pop())

    def __call__(self, func: Callable[PS, T]) -> Callable[PS, T]:
        @wraps(func)
        def wrapper(*args: PS.args, **kwargs: PS.kwargs) -> T:
            with self:
                return func(*args, **kwargs)

        return wrapper

    @classmethod
    def sequence(
        cls: type[restore], seq: MutableSequence[T],
    ) -> restore[MutableSequence[T], MutableSequence[T]]:
        """
        Example:
            >>> l = [1, 2, 3]
            >>>
            >>> restore_list = restore.sequence(l)
            >>> with restore_list:
            ...     print(l)
            ...     l.append(4)
            ...     print(l)
            ...     with restore_list:  # Reentrance
            ...         l[:] = 5, 6
            ...         print(l)
            ...     print(l)
            ...
            [1, 2, 3]
            [1, 2, 3, 4]
            [5, 6]
            [1, 2, 3, 4]
            >>> l
            [1, 2, 3]
        """

        def set_list(orig: MutableSequence[T], copy: Sequence[T]) -> None:
            orig[:] = copy

        return cls(seq, methodcaller('copy'), set_list)

    @classmethod
    def mapping(
        cls: type[restore],
        mpg: MutableMapping[K, V],
        keys: Collection[K] | None = None,
    ) -> restore[MutableMapping[K, V], Mapping[K, V | Any]]:
        """
        Example
        -------
        Whole-dict preservation:

        >>> d = {1: 2}

        >>> with restore.mapping(d):
        ...     print(d)
        ...     d[2] = 3
        ...     print(d)
        ...     d.clear()
        ...     d.update({1: 4, 3: 5})
        ...     print(d)
        ...
        {1: 2}
        {1: 2, 2: 3}
        {1: 4, 3: 5}
        >>> d
        {1: 2}

        Only preserving select key-value pairs:

        >>> d = {1: 2, 3: 4}

        >>> with restore.mapping(d, [1, 2]):
        ...     print(d)
        ...     d[1], d[2], d[3], d[4] = 5, 6, 7, 8
        ...     print(d)
        {1: 2, 3: 4}
        {1: 5, 3: 7, 2: 6, 4: 8}
        >>> d  # [1] reverted to 2, [2] reverted to none
        {1: 2, 3: 7, 4: 8}
        """

        def set_mapping(
            orig: MutableMapping[K, V], copy: Mapping[K, V],
        ) -> None:
            orig.clear()
            orig.update(copy)

        def get_subset(orig: Mapping[K, V]) -> Mapping[K, V | Sentinel]:
            if TYPE_CHECKING:
                assert keys is not None
            return {k: orig.get(k, sentinel) for k in keys}

        def set_subset(
            orig: MutableMapping[K, V], subset: Mapping[K, V | Sentinel],
        ) -> None:
            for key, value in subset.items():
                if value is sentinel:
                    orig.pop(key, None)
                else:
                    orig[key] = value

        class Sentinel(enum.Enum):
            sentinel = enum.auto()

        sentinel = Sentinel.sentinel
        if keys is None:
            return cls(mpg, methodcaller('copy'), set_mapping)
        else:
            return cls(mpg, get_subset, set_subset)

    @classmethod
    def instance_dict(
        cls: type[restore], obj: Any, attrs: Collection[str] | None = None,
    ) -> restore[MutableMapping[str, Any], Mapping[str, Any]]:
        """
        Example
        -------
        >>> class Obj:
        ...     def __init__(self, x, y):
        ...         self.x, self.y = x, y
        ...
        ...     def __repr__(self):
        ...         return 'Obj({0.x!r}, {0.y!r})'.format(self)
        ...
        >>>
        >>> obj = Obj(1, 2)
        >>>
        >>> with restore.instance_dict(obj):
        ...     print(obj)
        ...     obj.x, obj.y, obj.z = 4, 5, 6
        ...     print(obj, obj.z)
        ...
        Obj(1, 2)
        Obj(4, 5) 6
        >>> obj
        Obj(1, 2)
        >>> hasattr(obj, 'z')
        False
        """
        return cls.mapping(vars(obj), attrs)


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
        Convenience method for Formatting a call a la
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

    Note:
        If for whatever reason the handle cannot be closed, the function
        errors out and the tempfile is deleted.
    """
    handle, fname = mkstemp(**kwargs)
    path = Path(fname)
    try:
        os.close(handle)
        return path
    except Exception:
        path.unlink()
        raise
