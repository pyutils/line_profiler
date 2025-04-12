import functools
import inspect
import types


is_coroutine = inspect.iscoroutinefunction
is_generator = inspect.isgeneratorfunction


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
    `.enable_by_count()` and `.disable_by_count()` methods, rather than
    the `.enable()` and `.disable()` methods.

    Used by `line_profiler.line_profiler.LineProfiler` and
    `kernprof.ContextualProfile`.
    """
    def wrap_callable(self, func):
        """ Decorate a function to start the profiler on function entry and stop
        it on function exit.
        """
        if is_classmethod(func):
            wrapper = self.wrap_classmethod(func)
        elif is_staticmethod(func):
            wrapper = self.wrap_staticmethod(func)
        elif is_boundmethod(func):
            wrapper = self.wrap_boundmethod(func)
        elif is_partialmethod(func):
            wrapper = self.wrap_partialmethod(func)
        elif is_partial(func):
            wrapper = self.wrap_partial(func)
        elif is_property(func):
            wrapper = self.wrap_property(func)
        elif is_cached_property(func):
            wrapper = self.wrap_cached_property(func)
        elif is_coroutine(func):
            wrapper = self.wrap_coroutine(func)
        elif is_generator(func):
            wrapper = self.wrap_generator(func)
        else:
            wrapper = self.wrap_function(func)
        return wrapper

    def _wrap_callable_wrapper(self, wrapper, impl_attrs, *,
                               args=None, kwargs=None, name_attr=None):
        """
        Create a profiled wrapper object around callables based on an
        existing wrapper.

        Args:
            wrapper (W):
                Wrapper object around regular callables, like
                `property`, `staticmethod`, `functools.partial`, etc.
            impl_attrs (Sequence[str]):
                Attribute names whence to retrieve the individual
                callables to be wrapped and profiled, like `.fget`,
                `.fset`, and `.fdel` for `property`;
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
                else, the attribute of `wrapper` at the mapping value is
                used to populate the keyword arg at the mapping key.
            name_attr (Optional[str]):
                Optional attribute name whence to retrieve the name of
                `wrapper` to be carried over in the new wrapper, like
                `__name__` for `property` (Python 3.13+) and `attrname`
                for `functools.cached_property`.

        Returns:
            (W): new wrapper of the type of `wrapper`
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
        Wrap a class/static method to profile it.
        """
        return self._wrap_callable_wrapper(func, ('__func__',))

    wrap_classmethod = wrap_staticmethod = _wrap_class_and_static_method

    def wrap_boundmethod(self, func):
        """
        Wrap a bound method to profile it.
        """
        return self._wrap_callable_wrapper(func, ('__func__',),
                                           args=('__self__',))

    def _wrap_partial(self, func):
        """
        Wrap a `functools.partial[method]` to profile it.
        """
        return self._wrap_callable_wrapper(func, ('func',),
                                           args='args', kwargs='keywords')

    wrap_partial = wrap_partialmethod = _wrap_partial

    def wrap_property(self, func):
        """
        Wrap a property to profile it.
        """
        return self._wrap_callable_wrapper(func, ('fget', 'fset', 'fdel'),
                                           kwargs={'doc': '__doc__'},
                                           name_attr='__name__')

    def wrap_cached_property(self, func):
        """
        Wrap a `functools.cached_property` to profile it.
        """
        return self._wrap_callable_wrapper(func, ('func',),
                                           name_attr='attrname')

    def wrap_coroutine(self, func):
        """
        Wrap a Python 3.5 coroutine to profile it.
        """

        @functools.wraps(func)
        async def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = await func(*args, **kwds)
            finally:
                self.disable_by_count()
            return result

        return wrapper

    def wrap_generator(self, func):
        """ Wrap a generator to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            g = func(*args, **kwds)
            # The first iterate will not be a .send()
            self.enable_by_count()
            try:
                item = next(g)
            except StopIteration:
                return
            finally:
                self.disable_by_count()
            input_ = (yield item)
            # But any following one might be.
            while True:
                self.enable_by_count()
                try:
                    item = g.send(input_)
                except StopIteration:
                    return
                finally:
                    self.disable_by_count()
                input_ = (yield item)
        return wrapper

    def wrap_function(self, func):
        """ Wrap a function to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = func(*args, **kwds)
            finally:
                self.disable_by_count()
            return result
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
