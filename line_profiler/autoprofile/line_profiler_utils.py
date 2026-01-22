from __future__ import annotations

import inspect
from functools import cached_property, partial, partialmethod
from types import FunctionType, MethodType, ModuleType
from typing import TYPE_CHECKING, Any, Literal, overload

if TYPE_CHECKING:  # pragma: no cover
    from ..profiler_mixin import CLevelCallable, CythonCallable
    from ..scoping_policy import ScopingPolicy, ScopingPolicyDict


@overload
def add_imported_function_or_module(
        self, item: CLevelCallable | Any, *,
        scoping_policy: ScopingPolicy | str | ScopingPolicyDict | None = None,
        wrap: bool = False) -> Literal[0]:
    ...


@overload
def add_imported_function_or_module(
        self,
        item: (FunctionType | CythonCallable | type | partial | property
               | cached_property | MethodType | staticmethod | classmethod
               | partialmethod | ModuleType),
        *, scoping_policy: ScopingPolicy | str | ScopingPolicyDict | None = None,
        wrap: bool = False) -> Literal[0, 1]:
    ...


def add_imported_function_or_module(
        self, item: object, *,
        scoping_policy: ScopingPolicy | str | ScopingPolicyDict | None = None,
        wrap: bool = False) -> Literal[0, 1]:
    """
    Method to add an object to
    :py:class:`~.line_profiler.LineProfiler` to be profiled.

    This method is used to extend an instance of
    :py:class:`~.line_profiler.LineProfiler` so it can identify whether
    an object is a callable (wrapper), a class, or a module, and handle
    its profiling accordingly.

    Args:
        item (Union[Callable, Type, ModuleType]):
            Object to be profiled.
        scoping_policy (Union[ScopingPolicy, str, ScopingPolicyDict, \
None]):
            Whether (and how) to match the scope of members and decide
            on whether to add them:

            :py:class:`str` (incl. :py:class:`~.ScopingPolicy`):
                Strings are converted to :py:class:`~.ScopingPolicy`
                instances in a case-insensitive manner, and the same
                policy applies to all members.

            ``{'func': ..., 'class': ..., 'module': ...}``
                Mapping specifying individual policies to be enacted for
                the corresponding member types.

            :py:const:`None`
                The default, equivalent to
                :py:data:`~line_profiler.line_profiler\
.DEFAULT_SCOPING_POLICIES`.

            See :py:class:`line_profiler.line_profiler.ScopingPolicy`
            and :py:meth:`~.ScopingPolicy.to_policies` for details.
        wrap (bool):
            Whether to replace the wrapped members with wrappers which
            automatically enable/disable the profiler when called.

    Returns:
        1 if any function is added to the profiler, 0 otherwise.

    See also:
        :py:data:`~line_profiler.line_profiler\
.DEFAULT_SCOPING_POLICIES`,
        :py:meth:`.LineProfiler.add_callable()`,
        :py:meth:`.LineProfiler.add_module()`,
        :py:meth:`.LineProfiler.add_class()`,
        :py:class:`~.ScopingPolicy`,
        :py:meth:`ScopingPolicy.to_policies() \
<line_profiler.line_profiler.ScopingPolicy.to_policies>`
    """
    if inspect.isclass(item):
        count = self.add_class(item, scoping_policy=scoping_policy, wrap=wrap)
    elif inspect.ismodule(item):
        count = self.add_module(item, scoping_policy=scoping_policy, wrap=wrap)
    else:
        try:
            count = self.add_callable(item)
        except TypeError:
            count = 0
    if count:
        # Session-wide enabling means that we no longer have to wrap
        # individual callables to enable/disable the profiler when
        # they're called
        self.enable_by_count()
    return 1 if count else 0
