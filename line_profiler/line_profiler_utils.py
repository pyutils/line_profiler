"""
Miscellaneous utilities that :py:mod:`line_profiler` uses.
"""
import enum


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
    def _generate_next_value_(name, *_, **__):
        return name.lower()

    def __eq__(self, other):
        return self.value == other

    def __str__(self):
        return self.value


class StringEnum(getattr(enum, 'StrEnum', _StrEnumBase)):
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
    def _missing_(cls, value):
        if not isinstance(value, str):
            return None
        members = {name.casefold(): instance
                   for name, instance in cls.__members__.items()}
        return members.get(value.casefold())
