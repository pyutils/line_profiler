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
from typing import Any, Literal, TypeVar, cast
from typing_extensions import Concatenate, ParamSpec, Self

from .line_profiler_utils import CallbackRepr, make_tempfile
from . import _diagnostics as diagnostics


__all__ = ('Cleanup',)

PS = ParamSpec('PS')
K = TypeVar('K')
V = TypeVar('V')
_Stacks = dict[float, list[Callable[[], Any]]]
_StackContexts = list[_Stacks]
LogLevel = Literal['debug', 'info', 'warning', 'error', 'critical']


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
        self.cleanup(1, reason='context exit')

    # Cleanup methods

    def cleanup(
        self, levels: int | None = None, *, reason: str | None = None,
    ) -> None:
        """
        Pop cleanup callbacks from the internal stacks added via
        :py:meth:`~.add_cleanup` etc. and call them in order.

        Args:
            levels (int | None):
                Number of stack levels to clear; passing :py:const`None`
                clears the entire stack of callback stacks
            reason (str | None):
                Optional description of the reason for cleaning up
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
        for stacks in pop_contexts:
            self._cleanup(stacks, reason)

    def _cleanup(self, stacks: _Stacks, reason: str | None) -> None:
        r"""
        Example:
            >>> import re
            >>> from collections.abc import Callable
            >>> from dataclasses import dataclass
            >>> from typing import Any
            >>> from typing_extensions import Self

            >>> @dataclass
            ... class LogEntry:
            ...     level: str
            ...     msg: str
            ...
            ...     def compare(
            ...         self,
            ...         *,
            ...         level: str | None = None,
            ...         msg: str | None = None,
            ...         regex: bool = False,
            ...     ) -> bool:
            ...         for field, pat in [
            ...             ('level', level), ('msg', msg),
            ...         ]:
            ...             if pat is None:
            ...                 continue
            ...             value = getattr(self, field)
            ...             if regex:
            ...                 if not re.search(pat, value):
            ...                     return False
            ...             elif value != pat:
            ...                 return False
            ...         return True

            >>> class MyCleanup(Cleanup):
            ...     def __init__(self, *args, **kwargs) -> None:
            ...         super().__init__(*args, **kwargs)
            ...         self.debug_log: list[LogEntry] = []
            ...
            ...     def _debug_output(
            ...         self, msg: str, level: str = 'debug',
            ...     ) -> None:
            ...         self.debug_log.append(LogEntry(level, msg))

            >>> def good_callback(msg: Any) -> None:
            ...     print(msg)

            >>> def bad_callback(msg: Any) -> None:
            ...     raise RuntimeError(msg)

            >>> with MyCleanup() as cleanup:
            ...     cleanup.add_cleanup(good_callback, 1)
            ...     cleanup.add_cleanup(bad_callback, 2)
            ...     cleanup.add_cleanup(good_callback, 3)
            3
            1

            >>> log = cleanup.debug_log
            >>> assert len(log) >= 5, f'{log=!r}; {len(log)=!r}'

            >>> assert log[-5].compare(
            ...     level='debug',
            ...     msg='Starting cleanup '
            ...     '(context exit; 3 callback(s))...',
            ... ), f'{log[-5]=!r}'
            >>> assert log[-4].compare(
            ...     level='debug',
            ...     msg=r'- Cleanup succeeded \(1/3\): '
            ...     '.*good_callback.*3',
            ...     regex=True,
            ... ), f'{log[-4]=!r}'
            >>> assert log[-3].compare(
            ...     level='warning',
            ...     msg=r'- Cleanup failed \(2/3\): '
            ...     '.*bad_callback.*2.*: RuntimeError: 2',
            ...     regex=True,
            ... ), f'{log[-3]=!r}'
            >>> assert log[-2].compare(
            ...     level='debug',
            ...     msg=r'- Cleanup succeeded \(3/3\): '
            ...     '.*good_callback.*1.*',
            ...     regex=True,
            ... ), f'{log[-2]=!r}'
            >>> assert log[-1].compare(
            ...     level='debug',
            ...     msg='... cleanup completed '
            ...     '(context exit; 3 callback(s))',
            ... ), f'{log[-1]=!r}'

            >>> with MyCleanup() as cleanup:
            ...     pass
            >>> log = cleanup.debug_log
            >>> assert len(log) == 1, f'{log=!r}; {len(log)=!r}'
            >>> assert log[-1].compare(
            ...     msg='Cleanup aborted (context exit; 0 callback(s))',
            ... ), f'{log[-1]=!r}'
        """
        log = self._debug_output
        # Note: the doctest is mainly here for coverage purposes.
        ncallbacks_total = sum(len(stack) for stack in stacks.values())
        note = f'{ncallbacks_total} callback(s)'
        if reason:
            note = f'{reason}; {note}'
        if not ncallbacks_total:
            log(f'Cleanup aborted ({note})')
            return
        # Bookend the cleanup loop with log messages to help detect if
        # child processes are prematurely terminated
        log(f'Starting cleanup ({note})...')
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
                    success, state = False, 'failed'
                    msg = f'{callback_repr}: {type(e).__name__}: {e}'
                else:
                    success, state, msg = True, 'succeeded', f'{callback_repr}'
                msg = (
                    f'- Cleanup {state} '
                    f'({ncallbacks_run}/{ncallbacks_total}): {msg}'
                )
                log(msg, 'debug' if success else 'warning')
        log(f'... cleanup completed ({note})')

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
                are invoked BEFORE those with lower values
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
        self, mapping: MutableMapping[K, V], updates: Mapping[K, V],
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
            msg = self._format_mapping_update_message(mapping, key, change)
            self._debug_output(msg)
            mapping[key] = value

    def make_tempfile(
        self, *, delete: bool = True, priority: float = 0, **kwargs
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
        self._debug_output(self._format_tempfile_creation_message(path))
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
            >>> class Object:
            ...     pass  # Allow setting arbitrary attributes
            ...
            >>>
            >>> obj = Object()
            >>> obj.foo = 1
            >>> with Cleanup() as cleanup:
            ...     cleanup.patch(obj, 'foo', 2)
            ...     cleanup.patch(obj, 'bar', 3)
            ...     cleanup.patch(obj, 'baz', 4, cleanup=False)
            ...     assert obj.foo == 2
            ...     assert obj.bar == 3
            ...     assert obj.baz == 4  # Not restored
            ...
            >>> assert obj.foo == 1
            >>> assert not hasattr(obj, 'bar')
            >>> assert obj.baz == 4
        """
        if cleanup:
            add_cleanup: Callable[
                Concatenate[Callable[..., Any], float, ...], Any
            ] = self.add_cleanup_with_priority
        else:
            # ... yeah gotta disagree with flake8, a lambda makes
            # perfect sense here
            add_cleanup = lambda *_, **__: None  # noqa: E731
        _, should_be_restored, old_value = self._get_attribute_info(obj, attr)
        if should_be_restored:
            add_cleanup(setattr, priority, obj, attr, old_value)
        else:
            add_cleanup(delattr, priority, obj, attr)
        setattr(obj, attr, value)
        if name is None:
            name = self._get_name(obj)
        msg = 'Patched `{}.{}` -> `{}`'.format(name, attr, value)
        self._debug_output(msg)

    # Helper methods

    @staticmethod
    def _get_attribute_info(obj: Any, attr: str) -> tuple[bool, bool, Any]:
        """
        Returns:
            result (tuple[bool, bool, Any]):
                3-tuple of the following:

                - Whether the object has the attribute

                - Whether the present attribute value (if any) should be
                  restored if changed

                - The attribute that should be restored (if any)
        """
        try:
            value = getattr(obj, attr)
        except AttributeError:
            return False, False, None

        try:
            static_value = getattr_static(obj, attr)
        except AttributeError:
            has_static = False
        else:
            has_static = True

        if isinstance(obj, type) and has_static:
            # Special-case classes: only restore if value not inherited
            # from a base class
            return True, (attr in vars(obj)), static_value

        try:
            inherited_value = getattr(type(obj), attr)
        except AttributeError:
            # Two cases:
            # - `has_static=True`: `value` is instance-bound (e.g. in
            #   `vars()`)
            #   -> Should be restored
            # - `has_static=False`: `value` is calculated dynamically
            #   (via `.__getattr[ibute]__()`)
            return True, has_static, value

        # If it is a settable data descriptor, reset it; else, leave it
        # alone (i.e. delete the instance-bound override we assign to)
        InheritedDescriptorType = type(inherited_value)
        if not callable(getattr(InheritedDescriptorType, '__set__', None)):
            is_settable = False
        elif issubclass(InheritedDescriptorType, property):
            # Special-case properties
            is_settable = inherited_value.fset is not None
        else:
            is_settable = True
        return True, is_settable, value

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

    def _debug_output(self, msg: str, /, level: LogLevel = 'debug') -> None:
        """
        Write debugging output.

        Note:
            This default implementation just writes to the logger at the
            specified level.
        """
        log_func = getattr(diagnostics.log, level)
        log_func(msg)

    @staticmethod
    def _format_mapping_update_message(
        mapping: Mapping[K, Any], key: K, change: str, /,
    ) -> str:
        """
        Helper method used by :py:meth:`.update_mapping` to format debug
        messages.
        """
        mapping_repr = object.__repr__(mapping)
        return f'Update {mapping_repr}[{key!r}]: {change}'

    @staticmethod
    def _format_tempfile_creation_message(path: Path) -> str:
        """
        Helper method used by :py:meth:`.make_tempfile` to format debug
        messages.
        """
        return f'Created tempfile: {path}'

    @property
    def _current_context(self) -> _Stacks:
        try:
            return self._contexts[-1]
        except IndexError:
            ctx: _Stacks = {}
            self._contexts.append(ctx)
            return ctx
