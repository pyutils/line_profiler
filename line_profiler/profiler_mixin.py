import functools
import inspect
import types
from sys import version_info
from warnings import warn
from .scoping_policy import ScopingPolicy


is_coroutine = inspect.iscoroutinefunction
is_function = inspect.isfunction
is_generator = inspect.isgeneratorfunction
is_async_generator = inspect.isasyncgenfunction

# These objects are callables, but are defined in C(-ython) so we can't
# handle them anyway
C_LEVEL_CALLABLE_TYPES = (types.BuiltinFunctionType,
                          types.BuiltinMethodType,
                          types.ClassMethodDescriptorType,
                          types.MethodDescriptorType,
                          types.MethodWrapperType,
                          types.WrapperDescriptorType)

# Can't line-profile Cython in 3.12
_CANNOT_LINE_TRACE_CYTHON = (3, 12) <= version_info < (3, 13, 0, 'beta', 1)


def is_c_level_callable(func):
    """
    Returns:
        func_is_c_level (bool):
            Whether a callable is defined at the C-level (and is thus
            non-profilable).
    """
    return isinstance(func, C_LEVEL_CALLABLE_TYPES)


def is_cython_callable(func):
    if not callable(func):
        return False
    # Note: don't directly check against a Cython function type, since
    # said type depends on the Cython version used for building the
    # Cython code;
    # just check for what is common between Cython versions
    return (type(func).__name__
            in ('cython_function_or_method', 'fused_cython_function'))


def is_classmethod(f):
    return isinstance(f, classmethod)


def is_staticmethod(f):
    return isinstance(f, staticmethod)


def is_boundmethod(f):
    return isinstance(f, types.MethodType)


def is_partialmethod(f):
    return isinstance(f, functools.partialmethod)


def is_partial(f):
    return isinstance(f, functools.partial)


def is_property(f):
    return isinstance(f, property)


def is_cached_property(f):
    return isinstance(f, functools.cached_property)


class ByCountProfilerMixin:
    """
    Mixin class for profiler methods built around the
    :py:meth:`!enable_by_count()` and :py:meth:`!disable_by_count()`
    methods, rather than the :py:meth:`!enable()` and
    :py:meth:`!disable()` methods.

    Used by :py:class:`line_profiler.line_profiler.LineProfiler` and
    :py:class:`kernprof.ContextualProfile`.
    """
    def wrap_callable(self, func):
        """
        Decorate a function to start the profiler on function entry and
        stop it on function exit.
        """
        if is_classmethod(func):
            return self.wrap_classmethod(func)
        if is_staticmethod(func):
            return self.wrap_staticmethod(func)
        if is_boundmethod(func):
            return self.wrap_boundmethod(func)
        if is_partialmethod(func):
            return self.wrap_partialmethod(func)
        if is_partial(func):
            return self.wrap_partial(func)
        if is_property(func):
            return self.wrap_property(func)
        if is_cached_property(func):
            return self.wrap_cached_property(func)
        if is_async_generator(func):
            return self.wrap_async_generator(func)
        if is_coroutine(func):
            return self.wrap_coroutine(func)
        if is_generator(func):
            return self.wrap_generator(func)
        if isinstance(func, type):
            return self.wrap_class(func)
        if callable(func):
            return self.wrap_function(func)
        raise TypeError(f'func = {func!r}: does not look like a callable or '
                        'callable wrapper')

    @classmethod
    def get_underlying_functions(cls, func):
        """
        Get the underlying function objects of a callable or an adjacent
        object.

        Returns:
            funcs (list[Callable])
        """
        return cls._get_underlying_functions(func)

    @classmethod
    def _get_underlying_functions(cls, func, seen=None, stop_at_classes=False):
        if seen is None:
            seen = set()
        get_underlying = functools.partial(
            cls._get_underlying_functions,
            seen=seen, stop_at_classes=stop_at_classes)
        if any(check(func)
               for check in (is_boundmethod, is_classmethod, is_staticmethod)):
            return get_underlying(func.__func__)
        if any(check(func)
               for check in (is_partial, is_partialmethod, is_cached_property)):
            return get_underlying(func.func)
        if is_property(func):
            result = []
            for impl in func.fget, func.fset, func.fdel:
                if impl is not None:
                    result.extend(get_underlying(impl))
            return result
        if isinstance(func, type):
            if stop_at_classes:
                return [func]
            result = []
            get_filter = cls._class_scoping_policy.get_filter
            func_check = get_filter(func, 'func')
            cls_check = get_filter(func, 'class')
            for member in vars(func).values():
                try:
                    member_funcs = get_underlying(member, stop_at_classes=True)
                except TypeError:
                    continue
                for impl in member_funcs:
                    is_type = isinstance(impl, type)
                    check = cls_check if is_type else func_check
                    if not check(impl):
                        continue
                    if is_type:
                        result.extend(get_underlying(impl))
                    else:
                        result.append(impl)
            return result
        if not callable(func):
            raise TypeError(f'func = {func!r}: '
                            f'cannot get functions from {type(func)} objects')
        if id(func) in seen:
            return []
        seen.add(id(func))
        if is_function(func):
            return [func]
        if is_cython_callable(func):
            return [] if _CANNOT_LINE_TRACE_CYTHON else [func]
        if is_c_level_callable(func):
            return []
        func = type(func).__call__
        if is_c_level_callable(func):  # Can happen with builtin types
            return []
        return [func]

    def _wrap_callable_wrapper(self, wrapper, impl_attrs, *,
                               args=None, kwargs=None, name_attr=None):
        """
        Create a profiled wrapper object around callables based on an
        existing wrapper.

        Args:
            wrapper (W):
                Wrapper object around other callables, like
                :py:class:`property`, :py:func:`staticmethod`,
                :py:func:`functools.partial`, etc.
            impl_attrs (Sequence[str]):
                Attribute names whence to retrieve the individual
                callables to be wrapped and profiled, like ``.fget``,
                ``.fset``, and ``.fdel`` for :py:class:`property`;
                the retrieved values are wrapped and passed as
                positional arguments to the wrapper constructor.
            args (Optional[str | Sequence[str]]):
                Optional attribute name or names whence to retrieve
                extra positional arguments to pass to the wrapper
                constructor;
                if a single name, the retrieved value is unpacked;
                else, each name corresponds to one extra positional arg.
            kwargs (Optional[str | Mapping[str, str]]):
                Optional attribute name or name mapping whence to
                retrieve extra keyword arguments to pass to the wrapper
                constructor;
                if a single name, the retrieved values is unpacked;
                else, the attribute of ``wrapper`` at the mapping value
                is used to populate the keyword arg at the mapping key.
            name_attr (Optional[str]):
                Optional attribute name whence to retrieve the name of
                ``wrapper`` to be carried over in the new wrapper, like
                ``.__name__`` for :py:class:`property` (Python 3.13+)
                and ``.attrname`` for
                :py:func:`functools.cached_property`.

        Returns:
            new_wrapper (W):
                New wrapper of the type of ``wrapper``
        """
        # Wrap implementations
        impls = [getattr(wrapper, attr) for attr in impl_attrs]
        new_impls = [None if impl is None else self.wrap_callable(impl)
                     for impl in impls]

        # Get additional init args for the constructor
        if args is None:
            init_args = ()
        elif isinstance(args, str):
            init_args = getattr(wrapper, args)
        else:
            init_args = [getattr(wrapper, attr) for attr in args]
        if kwargs is None:
            init_kwargs = {}
        elif isinstance(kwargs, str):
            init_kwargs = getattr(wrapper, kwargs)
        else:
            init_kwargs = {}
            for name, attr in kwargs.items():
                try:
                    init_kwargs[name] = getattr(wrapper, attr)
                except AttributeError:
                    pass

        new_wrapper = type(wrapper)(*new_impls, *init_args, **init_kwargs)

        # Metadata: descriptor name, instance dict
        if name_attr:
            try:
                setattr(new_wrapper, name_attr, getattr(wrapper, name_attr))
            except AttributeError:
                pass
        try:
            old_vars = vars(wrapper)
            new_vars = vars(new_wrapper)
        except TypeError:  # Object doesn't necessarily have a dict
            pass
        else:
            for key, value in old_vars.items():
                new_vars.setdefault(key, value)

        return new_wrapper

    def _wrap_class_and_static_method(self, func):
        """
        Wrap a :py:func:`classmethod` or :py:func:`staticmethod` to
        profile it.
        """
        return self._wrap_callable_wrapper(func, ('__func__',))

    wrap_classmethod = wrap_staticmethod = _wrap_class_and_static_method

    def wrap_boundmethod(self, func):
        """
        Wrap a :py:class:`types.MethodType` to profile it.
        """
        return self._wrap_callable_wrapper(func, ('__func__',),
                                           args=('__self__',))

    def _wrap_partial(self, func):
        """
        Wrap a :py:func:`functools.partial` or
        :py:class:`functools.partialmethod` to profile it.
        """
        return self._wrap_callable_wrapper(func, ('func',),
                                           args='args', kwargs='keywords')

    wrap_partial = wrap_partialmethod = _wrap_partial

    def wrap_property(self, func):
        """
        Wrap a :py:class:`property` to profile it.
        """
        return self._wrap_callable_wrapper(func, ('fget', 'fset', 'fdel'),
                                           kwargs={'doc': '__doc__'},
                                           name_attr='__name__')

    def wrap_cached_property(self, func):
        """
        Wrap a :py:func:`functools.cached_property` to profile it.
        """
        return self._wrap_callable_wrapper(func, ('func',),
                                           name_attr='attrname')

    def wrap_async_generator(self, func):
        """
        Wrap an async generator function to profile it.
        """
        # Prevent double-wrap
        if self._already_a_wrapper(func):
            return func

        @functools.wraps(func)
        async def wrapper(*args, **kwds):
            g = func(*args, **kwds)
            # Async generators are started by `.asend(None)`
            input_ = None
            while True:
                self.enable_by_count()
                try:
                    item = (await g.asend(input_))
                except StopAsyncIteration:
                    return
                finally:
                    self.disable_by_count()
                input_ = (yield item)

        return self._mark_wrapper(wrapper)

    def wrap_coroutine(self, func):
        """
        Wrap a coroutine function to profile it.
        """
        # Prevent double-wrap
        if self._already_a_wrapper(func):
            return func

        @functools.wraps(func)
        async def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = await func(*args, **kwds)
            finally:
                self.disable_by_count()
            return result

        return self._mark_wrapper(wrapper)

    def wrap_generator(self, func):
        """
        Wrap a generator function to profile it.
        """
        # Prevent double-wrap
        if self._already_a_wrapper(func):
            return func

        @functools.wraps(func)
        def wrapper(*args, **kwds):
            g = func(*args, **kwds)
            # Generators are started by `.send(None)`
            input_ = None
            while True:
                self.enable_by_count()
                try:
                    item = g.send(input_)
                except StopIteration:
                    return
                finally:
                    self.disable_by_count()
                input_ = (yield item)

        return self._mark_wrapper(wrapper)

    def wrap_function(self, func):
        """
        Wrap a function to profile it.
        """
        # Prevent double-wrap
        if self._already_a_wrapper(func):
            return func

        @functools.wraps(func)
        def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = func(*args, **kwds)
            finally:
                self.disable_by_count()
            return result

        return self._mark_wrapper(wrapper)

    def wrap_class(self, func):
        """
        Wrap a class by wrapping all locally-defined callables and
        callable wrappers.

        Returns:
            func (type):
                The class passed in, with its locally-defined
                callables and wrappers wrapped.

        Warns:
            UserWarning
                If any of the locally-defined callables and wrappers
                cannot be replaced with the appropriate wrapper returned
                from :py:meth:`.wrap_callable()`.
        """
        get_filter = self._class_scoping_policy.get_filter
        func_check = get_filter(func, 'func')
        cls_check = get_filter(func, 'class')
        get_underlying = functools.partial(
            self._get_underlying_functions, stop_at_classes=True)
        members_to_wrap = {}
        for name, member in vars(func).items():
            try:
                impls = get_underlying(member)
            except TypeError:  # Not a callable (wrapper)
                continue
            if any((cls_check(impl)
                    if isinstance(impl, type) else
                    func_check(impl))
                   for impl in impls):
                members_to_wrap[name] = member
        self._wrap_namespace_members(func, members_to_wrap,
                                     warning_stack_level=2)
        return func

    def _wrap_namespace_members(
            self, namespace, members, *, warning_stack_level=2):
        wrap_failures = {}
        for name, member in members.items():
            wrapper = self.wrap_callable(member)
            if wrapper is member:
                continue
            try:
                setattr(namespace, name, wrapper)
            except (TypeError, AttributeError):
                # Corner case in case if a class/module don't allow
                # setting attributes (could e.g. happen with some
                # builtin/extension classes, but their method should be
                # in C anyway, so `.add_callable()` should've returned 0
                # and we shouldn't be here)
                wrap_failures[name] = member
        if wrap_failures:
            msg = (f'cannot wrap {len(wrap_failures)} attribute(s) of '
                   f'{namespace!r} (`{{attr: value}}`): {wrap_failures!r}')
            warn(msg, stacklevel=warning_stack_level)

    def _already_a_wrapper(self, func):
        return getattr(func, self._profiler_wrapped_marker, None) == id(self)

    def _mark_wrapper(self, wrapper):
        setattr(wrapper, self._profiler_wrapped_marker, id(self))
        return wrapper

    def run(self, cmd):
        """ Profile a single executable statment in the main namespace.
        """
        import __main__
        main_dict = __main__.__dict__
        return self.runctx(cmd, main_dict, main_dict)

    def runctx(self, cmd, globals, locals):
        """ Profile a single executable statement in the given namespaces.
        """
        self.enable_by_count()
        try:
            exec(cmd, globals, locals)
        finally:
            self.disable_by_count()
        return self

    def runcall(self, func, /, *args, **kw):
        """ Profile a single function call.
        """
        self.enable_by_count()
        try:
            return func(*args, **kw)
        finally:
            self.disable_by_count()

    def __enter__(self):
        self.enable_by_count()
        return self

    def __exit__(self, *_, **__):
        self.disable_by_count()

    _profiler_wrapped_marker = '__line_profiler_id__'
    _class_scoping_policy = ScopingPolicy.CHILDREN
