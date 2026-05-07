"""
Utilities for cleaning up after ourselves.
"""
from __future__ import annotations

from collections.abc import (
    Callable, Generator, Iterable, Mapping, MutableMapping,
)
from functools import partial
from inspect import getattr_static
from operator import setitem
from pathlib import Path
from typing import Any, TypeVar, cast
from typing_extensions import Concatenate, ParamSpec, Self

from .line_profiler_utils import CallbackRepr, make_tempfile
from . import _diagnostics as diagnostics


__all__ = ('Cleanup',)

PS = ParamSpec('PS')
K = TypeVar('K')
V = TypeVar('V')
_Stacks = dict[float, list[Callable[[], Any]]]
_StackContexts = list[_Stacks]


_CALLBACK_REPR_HELPER = CallbackRepr(maxother=cast(int, float('inf')))
_CALLBACK_REPR = _CALLBACK_REPR_HELPER.repr


class Cleanup:
    """
    Object which holds cleanup callbacks. Also provides convenience
    methods for creating tempfiles, updating mappings, and setting
    attributes on objects.
    """
    def __init__(self, *_, **__) -> None:
        self._contexts: _StackContexts = []

    def __enter__(self) -> Self:
        """
        Returns:
            The instance

        Note:
            This context manager is reentrant; entering the context
            create a new set of cleanup stacks, which is then cleaned up
            on :py:meth:`~.__exit__`.

        Example:
            >>> strings = []
            >>> add = strings.append
            >>> with Cleanup() as cleanup:
            ...     cleanup.add_cleanup(add, 'one')
            ...     # Increased priority
            ...     cleanup.add_cleanup_with_priority(add, 1, 'two')
            ...     add('three')
            ...     with cleanup:
            ...         # Decreased priority
            ...         cleanup.add_cleanup_with_priority(
            ...             add, -1, 'four',
            ...         )
            ...         cleanup.add_cleanup(add, 'five')
            ...         add('six')
            ...     add('seven')
            ...     # Increased priority
            ...     cleanup.add_cleanup_with_priority(add, 1, 'eight')
            ...
            >>> strings  # doctest: +NORMALIZE_WHITESPACE
            ['three', 'six', 'five', 'four', 'seven', 'eight', 'two',
             'one']
        """
        self._contexts.append({})
        return self

    def __exit__(self, *_, **__) -> Any:
        """
        Call ``~.cleanup(1)``, clearing the level of cleanup stacks we
        previously :py:meth:`~.__enter__`-ed into.
        """
        self.cleanup(1)

    # Cleanup methods

    def cleanup(self, levels: int | None = None) -> None:
        """
        Pop cleanup callbacks from the internal stacks added via
        :py:meth:`~.add_cleanup` etc. and call them in order.

        Args:
            levels (int | None):
                Number of stack levels to clear; passing :py:const`None`
                clears the entire stack of callback stacks
        """
        def pop_all_contexts(
            contexts: _StackContexts,
        ) -> Generator[_Stacks, None, None]:
            while contexts:
                yield contexts.pop()

        def pop_n_levels_of_contexts(
            contexts: _StackContexts, n: int,
        ) -> Generator[_Stacks, None, None]:
            for _ in range(n):
                try:
                    yield contexts.pop()
                except IndexError:  # Ran out of levels
                    return

        pop_contexts: Iterable[_Stacks]
        if levels is None:
            pop_contexts = pop_all_contexts(self._contexts)
        else:
            pop_contexts = pop_n_levels_of_contexts(self._contexts, levels)
        cleanup = partial(self._cleanup, self._debug_output)
        for stacks in pop_contexts:
            cleanup(stacks)

    @staticmethod
    def _cleanup(log: Callable[[str], Any], stacks: _Stacks) -> None:
        ncallbacks_total = sum(len(stack) for stack in stacks.values())
        if not ncallbacks_total:
            log('Cleanup aborted (no registered callbacks)')
            return
        # Bookend the cleanup loop with log messages to help detect if
        # child processes are prematurely terminated
        log(f'Starting cleanup ({ncallbacks_total} callback(s))...')
        ncallbacks_run = 0
        for priority in sorted(stacks, reverse=True):
            callbacks = stacks.pop(priority)
            while callbacks:
                callback = callbacks.pop()
                callback_repr = _CALLBACK_REPR(callback)
                ncallbacks_run += 1
                try:
                    callback()
                except Exception as e:
                    state = 'failed'
                    msg = f'{callback_repr}: {type(e).__name__}: {e}'
                else:
                    state, msg = 'succeeded', f'{callback_repr}'
                log(
                    f'- Cleanup {state} '
                    f'({ncallbacks_run}/{ncallbacks_total}): {msg}',
                )
        log(f'... cleanup completed ({ncallbacks_total} callback(s))')

    def add_cleanup(
        self, callback: Callable[PS, Any], *args: PS.args, **kwargs: PS.kwargs,
    ) -> None:
        """
        Shorthand for calling :py:meth:`~.add_cleanup_with_priority`
        with ``priority=0``, which should be considered the default.
        """
        self.add_cleanup_with_priority(callback, 0, *args, **kwargs)

    def add_cleanup_with_priority(
        self, callback: Callable[PS, Any], priority: float, /,
        *args: PS.args, **kwargs: PS.kwargs,
    ) -> None:
        """
        Add a cleanup callback to the internal stacks.

        Args:
            callback (Callable[..., Any]):
                Callback to be called at cleanup
            priority (float):
                Numeric priority value; callbacks with a HIGHER value
                are invoked BEFORE those with bigger values
            *args, **kwargs:
                Arguments ``callback`` should be called with

        Example:
            >>> strings = []
            >>> cleanup = Cleanup()
            >>> # Default priority
            >>> cleanup.add_cleanup(strings.append, 'first')
            >>> # Decreased priority
            >>> cleanup.add_cleanup_with_priority(
            ...     strings.append, -1, 'second',
            ... )
            >>> # Increased priority
            >>> cleanup.add_cleanup_with_priority(
            ...     strings.append, 1, 'third',
            ... )
            >>> cleanup.add_cleanup(strings.append, 'fourth')
            >>> assert not strings
            >>> cleanup.cleanup()
            >>> strings
            ['third', 'fourth', 'first', 'second']
        """
        if args or kwargs:
            callback = partial(callback, *args, **kwargs)
        self._current_context.setdefault(priority, []).append(callback)
        header = 'Cleanup callback added'
        if priority:
            header = f'{header} (priority: {priority})'
        self._debug_output(f'{header}: {_CALLBACK_REPR(callback)}')

    # Convenience methods

    def update_mapping(
        self,
        mapping: MutableMapping[K, V],
        updates: Mapping[K, V],
        *,
        _format_debug_msg: Callable[[Mapping[K, V], K, str], str] = (
            lambda mapping, key, change: 'Update {}[{!r}]: {}'.format(
                object.__repr__(mapping), key, change,
            )
        ),
    ) -> None:
        """
        Update a mapping with another and add cleanup callbacks to
        reverse them.

        Args:
            mapping (MutableMapping[K, V]):
                Mapping to be updated
            updates (Mapping[K, V]):
                Mapping containing the updates

        Example:
            >>> d1 = {1: 2, 3: 4}
            >>> d2 = d1.copy()
            >>> updates = {0: -1, 3: 5}
            >>> with Cleanup() as cleanup:
            ...     cleanup.update_mapping(d1, updates)
            ...     for key, value in updates.items():
            ...         assert d1[key] == value
            ...
            >>> assert d1 == d2
        """
        for key, value in updates.items():
            try:
                old = mapping[key]
            except KeyError:
                self.add_cleanup(mapping.pop, key, None)
                change = f'{value!r} (new)'
            else:
                self.add_cleanup(setitem, mapping, key, old)
                change = f'{old!r} -> {value!r}'
            self._debug_output(_format_debug_msg(mapping, key, change))
            mapping[key] = value

    def make_tempfile(
        self, *,
        delete: bool = True,
        priority: float = 0,
        _format_debug_msg: Callable[[Path], str] = (
            'Created tempfile: {}'.format
        ),
        **kwargs
    ) -> Path:
        """
        Create a fresh tempfile with :py:func:`tempfile.mkstemp`.

        Args:
            delete (bool):
                Whether to remove the file on cleanup
            priority (float):
                Cleanup priority (see
                :py:meth:`~.add_cleanup_with_priority`)
            **kwargs:
                Passed to :py:func:`tempfile.mkstemp`

        Returns:
            path (Path):
                Path to the created file.

        Example:
            >>> prefix, suffix = 'my_file_', '.txt'
            >>> with Cleanup() as cleanup:
            ...     path = cleanup.make_tempfile(
            ...         prefix=prefix, suffix=suffix,
            ...     )
            ...     assert path.exists()
            ...     assert path.name.startswith(prefix)
            ...     assert path.name.endswith(suffix)
            ...
            >>> assert not path.exists()
        """
        path = make_tempfile(**kwargs)
        self._debug_output(_format_debug_msg(path))
        if delete:
            self.add_cleanup_with_priority(
                path.unlink, priority, missing_ok=True,
            )
        return path

    def patch(
        self, obj: Any, attr: str, value: Any, *,
        name: str | None = None,
        static: bool = True,
        cleanup: bool = True,
        priority: float = 0,
    ) -> None:
        """
        Patch an attribute on an object.

        Args:
            obj (Any):
                Object to be patched
            attr (str):
                Name of the attribute
            value (Any):
                Value to be assigned to said attribute of ``obj``
            name (str | None):
                Optional name for ``obj`` to be used in debug messages
            static (bool):
                Whether to use :py:func:`inspect.getattr_static` to
                get the current value of the attribute
            cleanup (bool):
                Whether to reverse the patch (by resetting or deleting
                the attribute) on cleanup
            priority (float):
                Cleanup priority (see
                :py:meth:`~.add_cleanup_with_priority`)

        Example:
            >>> class Object(object):
            ...     pass  # Allow setting arbitrary attributes
            ...
            >>>
            >>> obj = Object()
            >>> obj.foo = 1
            >>> with Cleanup() as cleanup:
            ...     cleanup.patch(obj, 'foo', 2)
            ...     cleanup.patch(obj, 'bar', 3)
            ...     assert obj.foo == 2
            ...     assert obj.bar == 3
            ...
            >>> assert obj.foo == 1
            >>> assert not hasattr(obj, 'bar')
        """
        if cleanup:
            add_cleanup: Callable[
                Concatenate[Callable[..., Any], float, ...], Any
            ] = self.add_cleanup_with_priority
        else:
            # ... yeah gotta disagree with flake8, a lambda makes
            # perfect sense here
            add_cleanup = lambda *_, **__: None  # noqa: E731
        get_attribute = getattr_static if static else getattr

        try:
            old = get_attribute(obj, attr)
        except AttributeError:
            add_cleanup(delattr, priority, obj, attr)
        else:
            add_cleanup(setattr, priority, obj, attr, old)
        setattr(obj, attr, value)
        if name is None:
            name = self._get_name(obj)
        msg = 'Patched `{}.{}` -> `{}`'.format(name, attr, value)
        self._debug_output(msg)

    # Helper methods

    @staticmethod
    def _get_name(obj: Any, /) -> str:
        """
        Get an appropriate name for an arbitrary object.

        Example:
            >>> import textwrap
            >>>
            >>>
            >>> Cleanup._get_name(textwrap)
            'textwrap'
            >>> Cleanup._get_name(textwrap.dedent)
            'textwrap.dedent'
            >>> Cleanup._get_name(str)
            'str'
            >>> Cleanup._get_name(print)
            'print'
            >>> Cleanup._get_name(object())  # doctest: +ELLIPSIS
            '<object object at 0x...>'
        """
        if hasattr(obj, '__qualname__'):
            name = obj.__qualname__
        elif hasattr(obj, '__name__'):
            name = obj.__name__
        else:
            return repr(obj)
        if hasattr(obj, '__module__'):
            if obj.__module__ not in ('builtins', '__builtins__'):
                name = f'{obj.__module__}.{name}'
        return str(name)

    def _debug_output(self, msg: str, /) -> None:
        """
        Write debugging output.

        Note:
            This default implementation just writes to the logger.
        """
        diagnostics.log.debug(msg)

    @property
    def _current_context(self) -> _Stacks:
        try:
            return self._contexts[-1]
        except IndexError:
            ctx: _Stacks = {}
            self._contexts.append(ctx)
            return ctx
