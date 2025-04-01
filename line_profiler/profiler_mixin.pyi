import inspect


is_coroutine = inspect.iscoroutinefunction
is_generator = inspect.isgeneratorfunction


def is_classmethod(f) -> bool:
    ...


class ByCountProfilerMixin:

    def wrap_callable(self, func):
        ...

    def wrap_classmethod(self, func):
        ...

    def wrap_coroutine(self, func):
        ...

    def wrap_generator(self, func):
        ...

    def wrap_function(self, func):
        ...

    def run(self, cmd):
        ...

    def runctx(self, cmd, globals, locals):
        ...

    def runcall(self, func, /, *args, **kw):
        ...

    def __enter__(self):
        ...

    def __exit__(self, *_, **__):
        ...
