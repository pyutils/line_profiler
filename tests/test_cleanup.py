"""
Misc. tests for :py:mod:`line_profiler.cleanup`.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from inspect import getattr_static
from types import MethodType
from typing import Any, ClassVar, cast
from typing_extensions import Self

import pytest

from line_profiler.cleanup import Cleanup


class _Cases:
    def __init__(self) -> None:
        self.cases: dict[str, Callable[..., None]] = {}

    def add_case(
        self, name: str, func: Callable[..., Any], /, *args, **kwargs
    ) -> Self:
        if args or kwargs:
            func = partial(func, *args, **kwargs)
        self.cases[name] = func
        return self

    def run_case(self, name: str, /, *args, **kwargs) -> Self:
        self.cases[name](*args, **kwargs)
        return self


class Object:
    _dd: int

    def __init__(self, dd: int | None = None) -> None:
        if dd is not None:
            self.data_descriptor = dd

    def __dir__(self) -> list[str]:
        return [*object.__dir__(self), 'id']

    def __getattr__(self, attr: str) -> Any:
        if attr == 'dynamic_attr':
            return 0
        raise AttributeError(attr)

    def instance_method(self) -> tuple[Self, int]:
        return self, 1

    @classmethod
    def class_method(cls) -> tuple[type[Self], int]:
        return cls, 2

    @staticmethod
    def static_method() -> int:
        return 3

    @property
    def data_descriptor(self) -> int:
        try:
            return self._dd
        except AttributeError:
            self._dd = 4
            return self._dd

    @data_descriptor.setter
    def data_descriptor(self, dd: int) -> None:
        self._dd = dd

    attr_on_class: ClassVar[int] = 5


class InheritedObject(Object):
    pass


def _test_instance_method(
    obj: Object, expected_old: int, expected_new: int,
) -> None:
    assert expected_old != expected_new

    Class = type(obj)
    old_inst_method_impl = Class.instance_method
    impl_is_local = 'instance_method' in vars(Class)

    # ---------------------- Patch on class level ----------------------

    # Before patch
    assert obj.instance_method() == (obj, expected_old)

    with Cleanup() as cleanup:
        cleanup.patch(
            Class, 'instance_method', lambda self: (self, expected_new),
        )
        # Post patch
        assert 'instance_method' in vars(Class)
        assert obj.instance_method() == (obj, expected_new)
    # Patch reversal
    # - Instance level (dynamic)
    assert obj.instance_method() == (obj, expected_old)
    assert obj.instance_method == MethodType(old_inst_method_impl, obj)
    # - Class level
    assert ('instance_method' in vars(Class)) == impl_is_local
    assert Class.instance_method == old_inst_method_impl

    # ---------------------- Patch on inst. level ----------------------

    with Cleanup() as cleanup:
        cleanup.patch(
            obj, 'instance_method',
            MethodType(lambda self: (self, expected_new), obj),
        )
        # Post patch
        assert 'instance_method' in vars(obj)
        assert obj.instance_method() == (obj, expected_new)
    # Patch reversal
    # - Instance level (dynamic)
    assert obj.instance_method() == (obj, expected_old)
    assert obj.instance_method == MethodType(old_inst_method_impl, obj)
    # - Instance level (static)
    assert 'instance_method' not in vars(obj)
    assert getattr_static(obj, 'instance_method') == old_inst_method_impl


def _test_class_method(
    obj: Object, expected_old: int, expected_new: int,
) -> None:
    assert expected_old != expected_new

    Class = type(obj)
    old_cls_method_obj = cast(
        'classmethod[Object, ..., tuple[type[Object], int]]',
        getattr_static(Class, 'class_method'),
    )
    impl_is_local = 'class_method' in vars(Class)

    # ---------------------- Patch on class level ----------------------

    # Before patch
    assert obj.class_method() == (Class, expected_old)
    assert Class.class_method() == (Class, expected_old)
    with Cleanup() as cleanup:
        cleanup.patch(
            Class, 'class_method',
            classmethod(lambda Class: (Class, expected_new)),
        )
        # Post patch
        assert 'class_method' in vars(Class)
        assert Class.class_method() == (Class, expected_new)
        assert obj.class_method() == (Class, expected_new)
    # Patch reversal
    # - Instance level (dynamic)
    assert obj.class_method() == (Class, expected_old)
    assert obj.class_method == MethodType(old_cls_method_obj.__func__, Class)
    # - Class level
    assert ('class_method' in vars(Class)) == impl_is_local
    assert Class.class_method() == (Class, expected_old)
    assert Class.class_method == MethodType(old_cls_method_obj.__func__, Class)
    assert getattr_static(Class, 'class_method') is old_cls_method_obj

    # ---------------------- Patch on inst. level ----------------------

    with Cleanup() as cleanup:
        cleanup.patch(
            obj, 'class_method',
            MethodType(lambda Class: (Class, expected_new), Class),
        )
        # Post patch
        assert 'class_method' in vars(obj)
        assert obj.class_method() == (Class, expected_new)
    # Patch reversal
    # - Instance level (dynamic)
    assert obj.class_method() == (Class, expected_old)
    assert obj.class_method == MethodType(old_cls_method_obj.__func__, Class)
    # - Instance level (static)
    assert 'class_method' not in vars(obj)
    assert getattr_static(obj, 'class_method') == old_cls_method_obj


def _test_static_method(
    obj: Object, expected_old: int, expected_new: int,
) -> None:
    assert expected_old != expected_new

    Class = type(obj)
    old_st_method_obj = cast(
        'staticmethod[..., int]', getattr_static(Class, 'static_method'),
    )
    impl_is_local = 'static_method' in vars(Class)

    # ---------------------- Patch on class level ----------------------

    # Before patch
    assert obj.static_method() == expected_old
    assert Class.static_method() == expected_old
    with Cleanup() as cleanup:
        cleanup.patch(
            Class, 'static_method', staticmethod(lambda: expected_new),
        )
        # Post patch
        assert 'static_method' in vars(Class)
        assert Class.static_method() == expected_new
        assert obj.static_method() == expected_new
    # Patch reversal
    # - Instance level (dynamic)
    assert obj.static_method() == expected_old
    assert obj.static_method == old_st_method_obj.__func__
    # - Class level
    assert ('static_method' in vars(Class)) == impl_is_local
    assert Class.static_method() == expected_old
    assert Class.static_method == old_st_method_obj.__func__
    assert getattr_static(Class, 'static_method') is old_st_method_obj

    # ---------------------- Patch on inst. level ----------------------

    with Cleanup() as cleanup:
        cleanup.patch(obj, 'static_method', lambda: expected_new)
        # Post patch
        assert 'static_method' in vars(obj)
        assert obj.static_method() == expected_new
    # Patch reversal
    # - Instance level (dynamic)
    assert obj.static_method() == expected_old
    assert obj.static_method == old_st_method_obj.__func__
    # - Instance level (static)
    assert 'static_method' not in vars(obj)
    assert getattr_static(obj, 'static_method') == old_st_method_obj


def _test_property(obj: Object, expected: int) -> None:
    ncalls_wrapped_fget = 0

    def wrap_fget(self) -> int:
        nonlocal ncalls_wrapped_fget
        ncalls_wrapped_fget += 1
        return cast(Callable[[Object], int], old_prop.fget)(self)

    Class = type(obj)
    old_prop = cast(property, Class.data_descriptor)
    new_prop = old_prop.getter(wrap_fget)
    impl_is_local = 'data_descriptor' in vars(Class)
    # ---------------------- Patch on class level ----------------------

    # Before patch
    assert obj.data_descriptor == expected
    assert ncalls_wrapped_fget == 0
    with Cleanup() as cleanup:
        cleanup.patch(Class, 'data_descriptor', new_prop)
        # Post patch
        assert 'data_descriptor' in vars(Class)
        assert obj.data_descriptor == expected
        assert ncalls_wrapped_fget == 1  # Wrapped `.__get__()`
        assert obj.data_descriptor == expected
        assert ncalls_wrapped_fget == 2  # Wrapped `.__get__()`
    # Patch reversal
    # - Instance level (dynamic)
    assert obj.data_descriptor == expected
    assert ncalls_wrapped_fget == 2
    assert obj.data_descriptor == expected
    assert ncalls_wrapped_fget == 2
    # - Class level
    assert ('data_descriptor' in vars(Class)) == impl_is_local

    # ---------------------- Patch on inst. level ----------------------

    with Cleanup() as cleanup:
        cleanup.patch(obj, 'data_descriptor', expected + 2)
        # Post patch
        assert obj.data_descriptor == expected + 2
    # Patch reversal (same as normal instance attributes, as tested in
    # the doctest)
    assert obj.data_descriptor == expected


def _test_nonlocal_attr(
    obj: Object,
    expected_old: int,
    expected_new: int,
    name: str,
    dynamic: bool,
) -> None:
    assert expected_old != expected_new

    Class = type(obj)
    attr_is_on_class = name in vars(Class)

    # ---------------------- Patch on class level ----------------------

    # Before patch
    assert getattr(obj, name) == expected_old
    if not dynamic:
        assert getattr(Class, name) == expected_old
    with Cleanup() as cleanup:
        cleanup.patch(Class, name, expected_new)
        # Poast patch
        assert name in vars(Class)
        assert getattr(obj, name) == expected_new
        assert getattr(Class, name) == expected_new
    # Patch reversal
    # - Instance level (dynamic)
    assert getattr(obj, name) == expected_old
    # - Instance level (static)
    if dynamic:
        with pytest.raises(AttributeError):
            getattr_static(obj, name)
    else:
        assert getattr_static(obj, name) == expected_old
    # - Class level
    assert (name in vars(Class)) == attr_is_on_class
    if dynamic:
        assert not hasattr(Class, name)
    else:
        getattr(Class, name) == expected_old

    # ---------------------- Patch on inst. level ----------------------

    with Cleanup() as cleanup:
        cleanup.patch(obj, name, expected_new)
        # Post patch
        assert name in vars(obj)
        assert getattr(obj, name) == expected_new
    # Patch reversal
    # - Instance level (dynamic)
    assert name not in vars(obj)
    assert getattr(obj, name) == expected_old
    # - Instance level (static)
    if dynamic:
        with pytest.raises(AttributeError):
            getattr_static(obj, name)
    else:
        assert getattr_static(obj, name) == expected_old


_test_overridden_class_attr = partial(
    _test_nonlocal_attr, name='attr_on_class', dynamic=False,
)
_test_dynamic_attr = partial(
    _test_nonlocal_attr, name='dynamic_attr', dynamic=True,
)

_PATCHING_TEST_CASES = (
    _Cases()
    .add_case(
        'instance-method', _test_instance_method,
        expected_old=1, expected_new=2,
    )
    .add_case(
        'class-method', _test_class_method,
        expected_old=2, expected_new=3,
    )
    .add_case(
        'static-method', _test_static_method,
        expected_old=3, expected_new=4,
    )
    .add_case('data-descriptor', _test_property, expected=4)
    .add_case(
        'overridden-class-attr', _test_overridden_class_attr,
        expected_old=5, expected_new=6,
    )
    .add_case(
        'dynamic-attr', _test_dynamic_attr,
        expected_old=0, expected_new=1,
    )
)


@pytest.mark.parametrize('case', _PATCHING_TEST_CASES.cases)
def test_base_class_attr_patching(case: str) -> None:
    """
    Test :py:meth:`Cleanup.patch`-ing various attributes (descriptors,
    class/dynamic attributes) on (1) the base class they are defined in
    and (2) an instance thereof.

    Notes:
        The most basic use-cases with normal instance attributes are
        tested in the doctest of the method; this test is to test some
        of the remaining edge cases.
    """
    _PATCHING_TEST_CASES.run_case(case, Object())


@pytest.mark.parametrize('case', _PATCHING_TEST_CASES.cases)
def test_child_class_attr_patching(case: str) -> None:
    """
    Test :py:meth:`Cleanup.patch`-ing various attributes (descriptors,
    class/dynamic attributes) on (1) a class inheriting from the base
    class they are defined in and (2) an instance thereof.

    Notes:
        The most basic use-cases with normal instance attributes are
        tested in the doctest of the method; this test is to test some
        of the remaining edge cases.
    """
    _PATCHING_TEST_CASES.run_case(case, InheritedObject())
