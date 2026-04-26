"""
Utilities for cleaning up after ourselves.
"""
from __future__ import annotations

import sys
import os
from collections.abc import (
    Callable, Generator, Iterable, Mapping, MutableMapping, Collection,
)
from functools import partial
from operator import setitem
from reprlib import Repr
from textwrap import indent
from pathlib import Path
from types import MethodType
from typing import Any, TypeVar, TypedDict, cast
from typing_extensions import ParamSpec, Self, Unpack

from .line_profiler_utils import block_indent, make_tempfile
from . import _diagnostics as diagnostics


__all__ = ('Cleanup',)

PS = ParamSpec('PS')
K = TypeVar('K')
V = TypeVar('V')
# Note: `typing.AnyStr` deprecated since 3.13
AnyStr = TypeVar('AnyStr', str, bytes)
_Stacks = dict[float, list[Callable[[], Any]]]
_StackContexts = list[_Stacks]


class _ReprAttributes(TypedDict, total=False):
    """
    Note:
        We use this typed dict instead of directly supplying them in the
        :py:meth:`_CallbackRepr.__init__()` signature, because we don't
        want to bother with the default values there.
    """
    maxlevel: int
    maxtuple: int
    maxlist: int
    maxarray: int
    maxdict: int
    maxset: int
    maxfrozenset: int
    maxdeque: int
    maxstring: int
    maxlog: int
    maxother: int
    fillvalue: str
    indent: str | int | None


class _CallbackRepr(Repr):
    """
    :py:class:`reprlib.Repr` subclass to help with representing cleanup
    callbacks, special-casing certain relevant object types (see
    examples below).

    Example:
        >>> from functools import partial
        >>> from sys import version_info

        >>> class MyEnviron(dict):
        ...     def some_method(self) -> None:
        ...         ...
        ...
        >>>
        >>> class MyRepr(_CallbackRepr):
        ...     # Since we can't instantiate a new `os._Environ`, test
        ...     # the relevant method with a mock
        ...     repr_MyEnviron = _CallbackRepr.repr__Environ
        ...
        >>>
        >>> r = MyRepr(maxenv=3, maxargs=4, maxstring=15)

        Environ-dict formatting:

        >>> my_env = MyEnviron(
        ...     foo='1',
        ...     bar='2',
        ...     this_varname_is_long_but_isnt_truncated=(
        ...         "THIS VALUE IS TRUNCATED BECAUSE IT'S TOO LONG"
        ...     ),
        ...     baz='4',
        ... )
        >>> print(r.repr(my_env))
        environ({'foo': '1', 'bar': '2', \
'this_varname_is_long_but_isnt_truncated': 'THIS ... LONG', ...})

        Partial-object formatting:

        >>> r.maxenv = 0
        >>> print(r.repr(my_env.some_method))
        <bound method MyEnviron.some_method of environ({...})>

        Bound-method formatting:

        >>> r.maxargs = 0
        >>> callback_1 = partial(int, base=8)
        >>> print(r.repr(callback_1))
        functools.partial(<class 'int'>, ...)

        Indentation (Python 3.12+):

        >>> if version_info < (3, 12):
        ...     from pytest import skip
        ...
        ...     skip(
        ...         '`Repr.indent` not available on {}.{},{}'
        ...         .format(*sys.version_info)
        ...     )

        >>> r = MyRepr(maxenv=2, maxargs=4)
        >>> r.indent = 2
        >>> callback_1 = partial(int, base=8)
        >>> print(r.repr(callback_1))
        functools.partial(
          <class 'int'>,
          base=8,
        )

        >>> callback_2 = partial(min, 5, 4, 3, 2, 1)
        >>> r.indent = '----'
        >>> print(r.repr(callback_2))
        functools.partial(
        ----<built-in function min>,
        ----5,
        ----4,
        ----3,
        ----2,
        ----...,
        )

        >>> r.indent = '    '
        >>> r.maxenv = 2
        >>> print(r.repr(my_env.some_method))
        <bound method MyEnviron.some_method of environ({
                                                   'foo': '1',
                                                   'bar': '2',
                                                   ...,
                                               })>
    """
    def __init__(
        self,
        *,
        maxargs: int = 5,
        maxenv: int = 3,
        **kwargs: Unpack[_ReprAttributes]
    ) -> None:
        super().__init__()  # kwargs are 3.12+
        valid_kwargs = (
            _ReprAttributes.__optional_keys__
            | _ReprAttributes.__required_keys__
        )
        for k, v in kwargs.items():
            if k in valid_kwargs:
                setattr(self, k, v)
        self.maxargs = maxargs
        self.maxenv = maxenv

    def repr__Environ(self, env: os._Environ[AnyStr], level: int) -> str:
        get: Callable[[AnyStr], str] = partial(self.repr1, level=level-1)
        # Truncate envvar values, but not their names
        envvars = ['{!r}: {}'.format(k, get(v)) for k, v in env.items()]
        return self._format_items(envvars, ('environ({', '})'), self.maxenv)

    def repr_method(self, method: MethodType, level: int) -> str:
        instance = self.repr1(method.__self__, level-1)
        func = getattr(method.__func__, '__qualname__', '?')
        prefix, suffix = f'<bound method {func} of ', '>'
        # Take care of possible multi-line reprs
        return block_indent(instance, prefix) + suffix

    def repr_partial(self, ptl: partial, level: int) -> str:
        get: Callable[[Any], str] = partial(self.repr1, level=level-1)
        args = [get(arg) for arg in ptl.args]
        args.extend('{}={}'.format(k, get(v)) for k, v in ptl.keywords.items())
        args.insert(0, get(ptl.func))
        name = '{0.__module__}.{0.__qualname__}'.format(type(ptl))
        # The +1 is to account for `ptl.func`
        return self._format_items(args, (name + '(', ')'), self.maxargs + 1)

    def _format_items(
        self,
        items: Collection[str],
        delims: tuple[str, str],
        maxlen: int | None = None,
    ) -> str:
        start, end = delims
        if maxlen is not None and len(items) > maxlen:
            items = list(items)[:maxlen] + ['...']
        indent_prefix: str | None = self._get_indent()
        if indent_prefix is None or not items:
            return '{}{}{}'.format(start, ', '.join(items), end)
        return '\n'.join([
            start, *(indent(item + ',', indent_prefix) for item in items), end,
        ])

    if sys.version_info >= (3, 12):
        # Note: `.indent` only available since 3.12
        def _get_indent(self) -> str | None:
            indent = self.indent
            if indent is None or isinstance(indent, str):
                return indent
            return ' ' * indent
    else:
        @staticmethod
        def _get_indent() -> None:
            return None


_CALLBACK_REPR = _CallbackRepr(maxother=cast(int, float('inf'))).repr


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
            lambda path: f'Created tempfile: {path.name!r}'
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
        add_cleanup = self.add_cleanup if cleanup else (lambda *_, **__: None)
        try:
            old = getattr(obj, attr)
        except AttributeError:
            add_cleanup(delattr, obj, attr)
        else:
            add_cleanup(setattr, obj, attr, old)
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
