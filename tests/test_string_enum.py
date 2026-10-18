"""Behavioral compatibility checks for the Python 3.10+ string enum."""

import enum
import inspect

import pytest

from line_profiler.line_profiler import LineProfiler
from line_profiler.line_profiler_utils import StringEnum
from line_profiler.scoping_policy import ScopingPolicy


def test_string_enum_members():
    class Example(StringEnum):
        LOWER = enum.auto()
        MIXED = 'mixed'

    assert isinstance(Example.LOWER, Example)
    assert isinstance(Example.LOWER, str)
    assert Example.LOWER.value == 'lower'
    assert str(Example.LOWER) == 'lower'
    assert f'{Example.LOWER:>8}' == '   lower'
    assert Example.LOWER == 'lower'
    assert Example('LOWER') is Example.LOWER
    assert Example('mIxEd') is Example.MIXED
    with pytest.raises(ValueError):
        Example('other')


def test_scoping_policy_defaults_are_enum_members():
    signature = inspect.signature(LineProfiler._add_namespace)
    for name in [
        'func_scoping_policy',
        'class_scoping_policy',
        'module_scoping_policy',
    ]:
        default = signature.parameters[name].default
        assert default is ScopingPolicy.NONE
        assert isinstance(default, ScopingPolicy)
        assert callable(default.get_filter)
    assert ScopingPolicy('NONE') is ScopingPolicy.NONE
    assert ScopingPolicy.to_policies('children')['func'] is ScopingPolicy.CHILDREN
