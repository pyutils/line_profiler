import functools
import inspect


is_coroutine = inspect.iscoroutinefunction
is_generator = inspect.isgeneratorfunction


def is_classmethod(f):
    return isinstance(f, classmethod)


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
        elif is_coroutine(func):
            wrapper = self.wrap_coroutine(func)
        elif is_generator(func):
            wrapper = self.wrap_generator(func)
        else:
            wrapper = self.wrap_function(func)
        return wrapper

    def wrap_classmethod(self, func):
        """
        Wrap a classmethod to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = func.__func__(func.__class__, *args, **kwds)
            finally:
                self.disable_by_count()
            return result
        return wrapper

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
