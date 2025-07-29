from enum import auto
from types import FunctionType, ModuleType
from typing import overload, Literal, Callable, TypedDict
from .line_profiler_utils import StringEnum


class ScopingPolicy(StringEnum):
    EXACT = auto()
    CHILDREN = auto()
    DESCENDANTS = auto()
    SIBLINGS = auto()
    NONE = auto()

    @overload
    def get_filter(
            self,
            namespace: type | ModuleType,
            obj_type: Literal['func']) -> Callable[[FunctionType], bool]:
        ...

    @overload
    def get_filter(
            self,
            namespace: type | ModuleType,
            obj_type: Literal['class']) -> Callable[[type], bool]:
        ...

    @overload
    def get_filter(
            self,
            namespace: type | ModuleType,
            obj_type: Literal['module']) -> Callable[[ModuleType], bool]:
        ...

    @classmethod
    def to_policies(
        cls,
        policies: (str | 'ScopingPolicy' | 'ScopingPolicyDict'
                   | None) = None) -> '_ScopingPolicyDict':
        ...


ScopingPolicyDict = TypedDict('ScopingPolicyDict',
                              {'func': str | ScopingPolicy,
                               'class': str | ScopingPolicy,
                               'module': str | ScopingPolicy})
_ScopingPolicyDict = TypedDict('_ScopingPolicyDict',
                               {'func': str | ScopingPolicy,
                                'class': str | ScopingPolicy,
                                'module': str | ScopingPolicy})
