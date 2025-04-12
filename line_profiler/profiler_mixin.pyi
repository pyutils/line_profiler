import inspect
import typing


is_coroutine = inspect.iscoroutinefunction
is_generator = inspect.isgeneratorfunction


def is_classmethod(f) -> bool:
    ...


def is_staticmethod(f) -> bool:
    ...


def is_boundmethod(f) -> bool:
    ...


def is_partialmethod(f) -> bool:
    ...


def is_partial(f) -> bool:
    ...


def is_property(f) -> bool:
    ...


def is_cached_property(f) -> bool:
    ...


class ByCountProfilerMixin:

    def wrap_callable(self, func):
        ...

    def wrap_classmethod(self, func):
        ...

    def wrap_staticmethod(self, func):
        ...

    def wrap_boundmethod(self, func):
        ...

    def wrap_partialmethod(self, func):
        ...

    def wrap_partial(self, func):
        ...

    def wrap_property(self, func):
        ...

    def wrap_cached_property(self, func):
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
