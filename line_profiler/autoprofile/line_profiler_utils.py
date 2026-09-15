from __future__ import annotations

import inspect
import operator
from collections.abc import Callable, Collection, Iterable, MutableMapping
from functools import cached_property, partial, partialmethod
from importlib import import_module
from types import FunctionType, MethodType, ModuleType
from typing import TYPE_CHECKING, Any, Literal, overload

from .profmod_extractor import _should_profile_regular_import

if TYPE_CHECKING:  # pragma: no cover
    from ..profiler_mixin import CLevelCallable, CythonCallable
    from ..scoping_policy import ScopingPolicy, ScopingPolicyDict


@overload
def add_imported_function_or_module(
    self,
    item: CLevelCallable | Any,
    *,
    scoping_policy: ScopingPolicy | str | ScopingPolicyDict | None = None,
    wrap: bool = False,
) -> Literal[0]: ...


@overload
def add_imported_function_or_module(
    self,
    item: (
        FunctionType
        | CythonCallable
        | type
        | partial
        | property
        | cached_property
        | MethodType
        | staticmethod
        | classmethod
        | partialmethod
        | ModuleType
    ),
    *,
    scoping_policy: ScopingPolicy | str | ScopingPolicyDict | None = None,
    wrap: bool = False,
) -> Literal[0, 1]: ...


def add_imported_function_or_module(
    self,
    item: object,
    *,
    scoping_policy: ScopingPolicy | str | ScopingPolicyDict | None = None,
    wrap: bool = False,
) -> Literal[0, 1]:
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
        scoping_policy (Union[ScopingPolicy, str, ScopingPolicyDict, None]):
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


def add_star_import(
    self,
    import_from: str,
    targets: Collection[str] | None,
    namespace: MutableMapping[str, Any],
    **kwargs
) -> Literal[0, 1]:
    """
    Helper method for a :py:class:`~.line_profiler.LineProfiler` to
    handle star-imports (``from <module> import *``).

    Args:
        import_from (str):
            Module name to star-import from.
        targets (Collection[str] | None):
            Profile-on-import module targets; if :py:const:`None`, all
            the names imported by the star-import will be added to the
            profiler.
        namespace (MutableMapping[str, Any])
            Namespace into which the names from ``import_from`` should
            be imported.
        **kwargs
            Passed to :py:func:`.add_imported_function_or_module`.

    Returns:
        1 if any function is added to the profiler, 0 otherwise.
    """
    # Dynamically inspect the module to see which names would've been
    # inserted by a star-import
    module = import_module(import_from)
    try:
        all_names: list[str] | None = list(module.__all__)
    except AttributeError:
        # Note: we don't expect any other errors; the language standard
        # dictates that `__all__` has to be `Sequence[str]`, and any
        # other value would be an error upon star-import anyway
        all_names = None

    check_attr: Callable[[str], bool]
    sort_items: Callable[[Iterable[tuple[str, Any]]], list[tuple[str, Any]]]
    if all_names is None:  # Default behavior: take all public names
        check_attr = lambda attr: (  # noqa: E731
            not attr.startswith('_')
        )
        sort_items = list
    else:  # If we have a valid `.__all__`, take names therefrom
        check_attr = partial(operator.contains, set(all_names))
        sort_items = partial(
            sorted, key=lambda kv: all_names.index(kv[0]),
        )
    imported_names: dict[str, Any] = {
        attr: value
        for attr, value in inspect.getmembers(module)
        if check_attr(attr)
    }

    # Decide on which of the names to pass to the profiler
    # Note: the names should've already been inserted into the namespace
    # by the import statement itself, so this is just post-hoc
    # bookkeeping
    add: Callable[[Any], int]
    if hasattr(self, 'add_imported_function_or_module'):
        # Pseudo-method inserted by `.autoprofile.run()`
        add = partial(self.add_imported_function_or_module, **kwargs)
    else:
        add = partial(add_imported_function_or_module, self, **kwargs)
    count = 0
    sentinel = object()
    for name, value in sort_items(imported_names.items()):
        if not (
            targets is None
            or _should_profile_regular_import(
                targets, f'{import_from}.{name}',
            )
        ):
            # Check that the name should be profiled (if we have
            # constrained `targets`)
            continue
        if namespace.get(name, sentinel) is not value:  # nocover
            # Check that the actual object in the namespace is
            # consistent with what is imported
            continue
        count += add(value)
    return 1 if count else 0
