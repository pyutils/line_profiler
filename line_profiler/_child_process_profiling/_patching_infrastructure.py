from __future__ import annotations

import dataclasses
from collections.abc import (
    Callable, Collection, Generator, Mapping, Sequence, Set,
)
from functools import partial
from importlib import import_module
from inspect import getattr_static
from operator import attrgetter
from types import MappingProxyType as mappingproxy, ModuleType
from typing import (
    TYPE_CHECKING, Any, Literal, Protocol, TypeVar, cast, final, overload,
)
from typing_extensions import Self

from .cache import LineProfilingCache


__all__ = ('Patch', 'SingleModulePatch', 'Registry')

P = TypeVar('P', bound='Patch')


class Patch(Protocol):
    """
    Interface for patches.
    """
    def apply(
        self,
        cache: LineProfilingCache,
        *,
        cleanup: bool = True,
        **kwargs
    ) -> Any:
        """
        Apply the patch.

        Args:
            cache (LineProfilingCache):
                Session cache
            cleanup (bool):
                Whether ``cache.cleanup()`` should reverse the patch
            **kwargs
                Individual implementations should pick the ones they
                need and ignore the rest.

        Note:
            The patch is responsible for registering the requisite
            cleanup callbacks with ``cache`` so that it is reversed when
            ``cache.cleanup()`` is called.
        """
        ...

    @property
    def summary(self) -> Mapping[str, Set[str]]:
        """
        A mapping from dotted-path names of objects to the set of
        attributes patched thereon.
        """
        ...

    @property
    def priority(self) -> float | None:
        """
        Real number representing how the patch is to be prioritized. A
        patch with a HIGHER priority should be applied LATER, that way
        it gets to wrap wrappers created by patches applied earlier.
        """
        ...


@dataclasses.dataclass
class SingleModulePatch:
    """
    Patch to apply to a module component.

    Attributes:
        module (str):
            Name of the module.
        targets (dict[str,\
dict[str, Callable[[Any], Any] | Sequence[Callable[[Any], Any]]]]):
            Dictionary mapping (dot-chained) names in said module to
            a dictionary of patches; said patches dictionary should have
            the format of
            ``dict[simple_attribute, wrapper | [wrapper1, ...]]``. See
            Example for details.
        priority (float | None):
            Optional numerical value for how to prioritize the patch
            (see :py:class:`.Patch`)

    Example:
        Consider
        ``SingleModulePatch('foo', {'bar.baz': {'foobar': foofoo},\
'': {'spam': [ham, eggs]}})``.
        This instance would perform the following patches on the module
        ``foo``:

        - Replace ``foo.bar.baz.foobar`` with
          ``foofoo(foo.bar.baz.foobar)``

        - Replace ``foo.spam`` with ``eggs(ham(foo.spam))``;
          note that the two wrappers are applied in order to the
          original attribute.
    """
    module: str
    targets: dict[
        str, dict[str, Callable[[Any], Any] | Sequence[Callable[[Any], Any]]]
    ] = dataclasses.field(default_factory=dict)
    priority: float | None = None

    def add_target(
        self,
        target: str,
        patches: Mapping[
            str, Callable[[Any], Any] | Sequence[Callable[[Any], Any]]
        ],
    ) -> Self:
        """
        Convenience method for gradually constructing the patch with a
        fluent interface.

        Args:
            target (str):
                Dotted path to the object in :py:attr:`.module`
            patches (Mapping[str, Callable[[Any], Any] \
| Sequence[Callable[[Any], Any]]]):
                Mapping from patched attrbute names to the wrappers to
                apply thereto; sequences of wrappers are applied in
                order

        Returns:
            This instance
        """
        self.targets.setdefault(target, {}).update(patches)
        return self

    def add_method(
        self,
        target: str,
        method: str,
        wrapper: Callable[[Any], Any],
        methodtype: (
            type[classmethod] | type[staticmethod]
            | Literal['class', 'static'] | None
        ) = None,
    ) -> Self:
        """
        Convenience method for gradually constructing the patch with a
        fluent interface.

        Args:
            target (str):
                Dotted path to the object in :py:attr:`.module`
            method (str):
                Name of the (class, static, or instance) method to patch
            wrapper (Callable[[Any], Any]):
                Wrapping callable which takes the method-implementaion
                callable and returns a wrapper thereof
            methodtype (type[classmethod] | type[staticmethod] | \
Literal['class', 'static'] | None):
                Optional type of the method if not an instance method;
                the strings ``'class'`` and ``'static'`` are respective
                shorthands for :py:class:`classmethod` and
                :py:class:`staticmethod`

        Returns:
            This instance
        """
        wrappers: Callable[[Any], Any] | list[Callable[[Any], Any]]
        if methodtype is None:
            wrappers = wrapper
        else:
            if methodtype == 'class':
                methodtype = classmethod
            elif methodtype == 'static':
                methodtype = staticmethod
            wrappers = [attrgetter('__func__'), wrapper, methodtype]
        return self.add_target(target, {method: wrappers})

    def apply(
        self,
        cache: LineProfilingCache,
        *,
        cleanup: bool = True,
        static: bool = True,
        **_
    ) -> list[str]:
        """
        Apply the patch.

        Args:
            cache (LineProfilingCache):
                Session cache
            cleanup (bool):
                Whether ``cache.cleanup()`` should reverse the patch
            static (bool):
                Whether to use :py:func:`inspect.getattr_static` to
                retrieve to the attributes to be patched on the patch
                targets

        Returns:
            replacements (list[str]):
                Names of entities replaced
        """
        submod_name = self.module
        get_attribute = getattr_static if static else getattr
        result: list[str] = []
        try:
            mod = self.load_module()
        except ImportError:  # nocover
            return []

        for target in sorted(self.targets, key=len, reverse=True):
            if TYPE_CHECKING:
                # See `ty` issue #2572
                assert isinstance(target, str)
            if target:
                try:
                    obj: Any = attrgetter(target)(mod)
                except AttributeError:  # nocover
                    continue
                name = f'{submod_name}.{target}'
            else:
                obj, name = mod, submod_name
            replace = partial(cache.patch, obj, cleanup=cleanup, name=name)
            for method, method_wrappers in self.targets[target].items():
                if callable(method_wrappers):
                    method_wrappers = cast(
                        Sequence[Callable[[Any], Any]], (method_wrappers,),
                    )
                try:
                    impl = get_attribute(obj, method)
                except AttributeError:
                    continue
                for wrapper in method_wrappers:
                    impl = wrapper(impl)
                replace(method, impl)
                result.append(f'{name}.{method}')
        return result

    def load_module(self) -> ModuleType:
        """
        Returns:
            Module object :py:attr:`.module` points to
        """
        return import_module(self.module)

    @staticmethod
    def _join(s: str, *strs: str, sep: str = '.') -> str:
        return sep.join(string for string in (s, *strs) if string)

    @property
    def summary(self) -> mappingproxy[str, frozenset[str]]:
        """
        Summary of the dotted paths to the patched objects and their
        patched attributes
        """
        add_prefix = partial(self._join, self.module)
        return mappingproxy({
            add_prefix(target): frozenset(patches)
            for target, patches in self.targets.items()
        })


@final
class Registry(Mapping[str, Patch]):
    """
    Mapping subclass for managing patches.
    """
    def __init__(self) -> None:
        self._patches: dict[str, tuple[float, Patch]] = {}

    def __repr__(self) -> str:
        if self._patches:
            patches = ', '.join(
                f'{name!r} (priority {priority})' if priority else repr(name)
                for name, (priority, _) in self._patches.items()
            )
            patches = f'({len(self)} patch(es)): {patches}'
        else:
            patches = '(0 patches)'
        return f'<{type(self).__name__} @ {id(self):#x} {patches}>'

    def __getitem__(self, key: str) -> Patch:
        return self._patches[key][1]

    def __iter__(self) -> Generator[str, None, None]:
        for name, _ in self._iter_patches():
            yield name

    def __len__(self) -> int:
        return len(self._patches)

    def __contains__(self, key: Any) -> bool:
        return key in self._patches

    @overload
    def register(
        self, name: str, patch: P, *, priority: float | None = None,
    ) -> P:
        ...

    @overload
    def register(
        self, name: str, patch: None = None, *, priority: float | None = None,
    ) -> Patch:
        ...

    def register(
        self, name: str, patch: Patch | None = None, *,
        priority: float | None = None,
    ) -> Patch:
        """
        Register/look up a patch.

        Args:
            name (str):
                Name of the patch; patches named with leading double
                underscores are considered MANDATORY, and are applied no
                matter the user input (e.g. via
                ``apply(..., patches=...)`` or the config file).
            patch (Patch | None):
                Patch object to register; if not provided, look for the
                existing patch registered under the name.
            priority (float | None):
                Optional priority to assign to the patch; default is to
                look at :py:attr:`Patch.priority` and to resolve
                :py:const:`None` to 0.

        Returns:
            Patch object (``patch`` if provided, looked up otherwise)
        """
        if patch is not None:
            if priority is None:
                priority = patch.priority
            if not priority:
                priority = 0
            old_pri, stored = self._patches.setdefault(name, (priority, patch))
            if stored is not patch:
                raise ValueError(
                    f'name = {name!r}, patch = {patch!r}: '
                    f'name already in use by {stored!r}'
                )
            if old_pri != priority:  # Update priority
                self._patches[name] = priority, patch
        elif priority is not None:
            # Reassign priority of an existing patch
            self._patches[name] = priority, self[name]
        return self[name]

    def select(self, patches: Collection[str]) -> Registry:
        """
        Returns:
            New instance with the selected patches

        Note:
            Patches whose names are prefixed with double underscores are
            considered mandatory and are always selected.
        """
        new = Registry()
        new._patches.update(
            (name, patch_info) for name, patch_info in self._patches.items()
            if name.startswith('__') or name in patches
        )
        return new

    def _iter_patches(
        self, reverse: bool = False,
    ) -> Generator[tuple[str, Patch], None, None]:
        """
        Iterate over the available patches.

        Note:
            Since patches typically consists of function wrappers, and
            outer wrappers are both called first and responsible for
            calling inner wrappers, patches with a HIGHER priority are
            yielded LATER (or earlier if ``reverse=True``.
        """
        for _, patches in sorted(self._prioritized.items(), reverse=reverse):
            yield from patches.items()

    @property
    def summary(self) -> dict[str, frozenset[str]]:
        """
        Mapping from the names of the entities affected by the patches
        to sets of attributes patched thereon.
        """
        summaries = [patch.summary for patch in self.values()]
        return {
            target: frozenset().union(*(s.get(target, ()) for s in summaries))
            for target in frozenset().union(*summaries)
        }

    @property
    def _prioritized(self) -> dict[float, dict[str, Patch]]:
        """
        Note:
            This could've been a static attribute maintained by the
            :py:meth:`.register` method, but we don't call this a bunch
            so it isn't like we incur a lot of overhead by calculating
            it on-the-fly; and it's less error-prone this way.
        """
        result: dict[float, dict[str, Patch]] = {}
        for name, (priority, patch) in self._patches.items():
            result.setdefault(priority, {})[name] = patch
        return result
