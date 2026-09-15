"""
Miscellaneous utilities that :py:mod:`line_profiler` uses.
"""

from __future__ import annotations

import enum
from collections.abc import (
    Callable, Collection, Mapping, MutableMapping, MutableSequence, Sequence,
)
from functools import wraps
from operator import methodcaller
from typing import TYPE_CHECKING, Any, Generic, TypeVar, final
from typing_extensions import Self, ParamSpec


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
