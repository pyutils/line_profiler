import inspect


def add_imported_function_or_module(self, item, *,
                                    match_scope='siblings', wrap=False):
    """
    Method to add an object to `LineProfiler` to be profiled.

    This method is used to extend an instance of `LineProfiler` so it
    can identify whether an object is a callable (wrapper), a class, or
    a module, and handle its profiling accordingly.

    Args:
        item (Union[Callable, Type, ModuleType]):
            Object to be profiled.
        match_scope (Literal['exact', 'siblings', 'descendants',
                             'none']):
            Whether (and how) to match the scope of member classes to
            `item` (if a class or module) and decide on whether to add
            them:
            - 'exact': only add classes defined locally in the body of
              `item`
            - 'descendants': only add locally-defined classes and
              classes defined in submodules or locally-defined class
              bodies, and so on.
            - 'siblings': only add classes fulfilling 'descendants',
              or defined in the same module as `item` (if a class) or in
              sibling modules and subpackages to `item` (if a module)
            - 'none': don't check scopes and add all classes in the
              namespace
        wrap (bool):
            Whether to replace the wrapped members with wrappers which
            automatically enable/disable the profiler when called.

    Returns:
        1 if any function is added to the profiler, 0 otherwise.

    See also:
        `LineProfiler.add_callable()`, `.add_module()`, `.add_class()`
    """
    if inspect.isclass(item):
        count = self.add_class(item, match_scope=match_scope, wrap=wrap)
    elif inspect.ismodule(item):
        count = self.add_module(item, match_scope=match_scope, wrap=wrap)
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
