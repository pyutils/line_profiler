import inspect
from ..line_profiler import ScopingPolicy


def add_imported_function_or_module(
        self, item, *,
        scoping_policy=ScopingPolicy.SIBLINGS, wrap=False):
    """
    Method to add an object to
    :py:class:`~.line_profiler.LineProfiler` to be profiled.

    This method is used to extend an instance of
    :py:class:`~.line_profiler.LineProfiler` so it can identify whether
    an object is a callable (wrapper), a class, or a module, and handle
    its profiling accordingly.

    Args:
        item (Union[Callable, Type, ModuleType]):
            Object to be profiled.
        scoping_policy (Union[ScopingPolicy, str]):
            Whether (and how) to match the scope of member classes to
            ``item`` (if a class or module) and decide on whether to add
            them;
            see the documentation for :py:class:`~.ScopingPolicy` for
            details.
            Strings are converted to :py:class:`~.ScopingPolicy`
            instances in a case-insensitive manner.
            Can also be a mapping from the keys ``'func'``, ``'class'``,
            and ``'module'`` to :py:class:`~.ScopingPolicy` objects or
            strings convertible thereto, in which case different
            policies can be enacted for these object types.
        wrap (bool):
            Whether to replace the wrapped members with wrappers which
            automatically enable/disable the profiler when called.

    Returns:
        1 if any function is added to the profiler, 0 otherwise.

    See also:
        :py:meth:`.LineProfiler.add_callable()`,
        :py:meth:`.LineProfiler.add_module()`,
        :py:meth:`.LineProfiler.add_class()`
    """
    if inspect.isclass(item):
        count = self.add_class(item, scoping_policy=scoping_policy, wrap=wrap)
    elif inspect.ismodule(item):
        count = self.add_module(item, scoping_policy=scoping_policy, wrap=wrap)
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
