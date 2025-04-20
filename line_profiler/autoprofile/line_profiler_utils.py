import inspect


def add_imported_function_or_module(self, item, *, wrap=False):
    """
    Method to add an object to `LineProfiler` to be profiled.

    This method is used to extend an instance of `LineProfiler` so it
    can identify whether an object is a callable (wrapper), a class, or
    a module, and handle its profiling accordingly.

    Args:
        item (Union[Callable, Type, ModuleType]):
            Object to be profiled.
        wrap (bool):
            Whether to replace the wrapped members with wrappers which
            automatically enable/disable the profiler when called.

    Returns:
        1 if any function is added to the profiler, 0 otherwise.

    See also:
        `LineProfiler.add_callable()`, `.add_module()`, `.add_class()`
    """
    if inspect.isclass(item):
        count = self.add_class(item, wrap=wrap)
    elif inspect.ismodule(item):
        count = self.add_module(item, wrap=wrap)
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
