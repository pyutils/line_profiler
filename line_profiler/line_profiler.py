#!/usr/bin/env python
"""
This module defines the core :class:`LineProfiler` class as well as methods to
inspect its output. This depends on the :py:mod:`line_profiler._line_profiler`
Cython backend.
"""
import functools
import inspect
import linecache
import os
import pickle
import sys
import tempfile
import types
import warnings
from argparse import ArgumentError, ArgumentParser
from enum import auto
from typing import Union, TypedDict

try:
    from ._line_profiler import LineProfiler as CLineProfiler
except ImportError as ex:
    raise ImportError(
        'The line_profiler._line_profiler c-extension is not importable. '
        f'Has it been compiled? Underlying error is ex={ex!r}'
    )
from .line_profiler_utils import StringEnum
from .profiler_mixin import (ByCountProfilerMixin,
                             is_property, is_cached_property,
                             is_boundmethod, is_classmethod, is_staticmethod,
                             is_partial, is_partialmethod)


# NOTE: This needs to be in sync with ../kernprof.py and __init__.py
__version__ = '4.3.0'

# These objects are callables, but are defined in C so we can't handle
# them anyway
C_LEVEL_CALLABLE_TYPES = (types.BuiltinFunctionType,
                          types.BuiltinMethodType,
                          types.ClassMethodDescriptorType,
                          types.MethodDescriptorType,
                          types.MethodWrapperType,
                          types.WrapperDescriptorType)

#: Default scoping policies:
#:
#: * Profile sibling and descendant functions
#:   (:py:attr:`ScopingPolicy.SIBLINGS`)
#: * Descend ingo sibling and descendant classes
#:   (:py:attr:`ScopingPolicy.SIBLINGS`)
#: * Don't descend into modules (:py:attr:`ScopingPolicy.EXACT`)
DEFAULT_SCOPING_POLICIES = types.MappingProxyType({'func': 'siblings',
                                                   'class': 'siblings',
                                                   'module': 'exact'})

is_function = inspect.isfunction


def is_c_level_callable(func):
    """
    Returns:
        func_is_c_level (bool):
            Whether a callable is defined at the C level (and is thus
            non-profilable).
    """
    return isinstance(func, C_LEVEL_CALLABLE_TYPES)


def load_ipython_extension(ip):
    """ API for IPython to recognize this module as an IPython extension.
    """
    from .ipython_extension import LineProfilerMagics
    ip.register_magics(LineProfilerMagics)


def _get_underlying_functions(func):
    """
    Get the underlying function objects of a callable or an adjacent
    object.

    Returns:
        funcs (list[Callable])
    """
    if any(check(func)
           for check in (is_boundmethod, is_classmethod, is_staticmethod)):
        return _get_underlying_functions(func.__func__)
    if any(check(func)
           for check in (is_partial, is_partialmethod, is_cached_property)):
        return _get_underlying_functions(func.func)
    if is_property(func):
        result = []
        for impl in func.fget, func.fset, func.fdel:
            if impl is not None:
                result.extend(_get_underlying_functions(impl))
        return result
    if not callable(func):
        raise TypeError(f'func = {func!r}: '
                        f'cannot get functions from {type(func)} objects')
    if is_function(func):
        return [func]
    if is_c_level_callable(func):
        return []
    return [type(func).__call__]


class _WrapperInfo:
    """
    Helper object for holding the state of a wrapper function.

    Attributes:
        func (types.FunctionType):
            The function it wraps.
        profiler_id (int)
            ID of the `LineProfiler`.
    """
    def __init__(self, func, profiler_id):
        self.func = func
        self.profiler_id = profiler_id


class ScopingPolicy(StringEnum):
    """
    :py:class:`StrEnum` for scoping policies, that is, how it is
    decided whether to:

    * Profile a function found in a namespace (a class or a module), and
    * Descend into nested namespaces so that their methods and functions
      are profiled,

    when using :py:meth:`LineProfiler.add_class`,
    :py:meth:`LineProfiler.add_module`, and
    :py:func:`~.add_imported_function_or_module()`.

    Available policies are:

    :py:attr:`ScopingPolicy.EXACT`
        Only profile *functions* found in the namespace fulfilling
        :py:attr:`ScopingPolicy.CHILDREN` as defined below, without
        descending into nested namespaces

    :py:attr:`ScopingPolicy.CHILDREN`
        Only profile/descend into *child* objects, which are:

        * Classes and functions defined *locally* in the very
          module, or in the very class as its "inner classes" and
          methods
        * Direct submodules, in case when the namespace is a module
          object representing a package

    :py:attr:`ScopingPolicy.DESCENDANTS`
        Only profile/descend into *descendant* objects, which are:

        * Child classes, functions, and modules, as defined above in
          :py:attr:`ScopingPolicy.CHILDREN`
        * Their child classes, functions, and modules, ...
        * ... and so on

        Note:
            Since imported submodule module objects are by default
            placed into the namespace of their parent-package module
            objects, this functions largely identical to
            :py:attr:`ScopingPolicy.CHILDREN` for descent from module
            objects into other modules objects.

    :py:attr:`ScopingPolicy.SIBLINGS`
        Only profile/descend into *sibling* and descendant objects,
        which are:

        * Descendant classes, functions, and modules, as defined above
          in :py:attr:`ScopingPolicy.DESCENDANTS`
        * Classes and functions (and descendants thereof) defined in the
          same parent namespace to this very class, or in modules (and
          subpackages and their descendants) sharing a parent package
          to this very module
        * Modules (and subpackages and their descendants) sharing a
          parent package, when the namespace is a module

    :py:attr:`ScopingPolicy.NONE`
        Don't check scopes;  profile all functions found in the local
        namespace of the class/module, and descend into all nested
        namespaces recursively

        Note:
            This is probably a *very* bad idea for module scoping,
            potentially resulting in accidentally recursing through a
            significant portion of loaded modules;
            proceed with care.

    Note:
        Other than :py:class:`enum.Enum` methods starting and ending
        with single underscores (e.g. :py:meth:`!_missing_`), all
        methods prefixed with a single underscore are to be considered
        implementation details.
    """
    EXACT = auto()
    CHILDREN = auto()
    DESCENDANTS = auto()
    SIBLINGS = auto()
    NONE = auto()

    # Verification

    def __init_subclass__(cls, *args, **kwargs):
        """
        Call :py:meth:`_check_class`.
        """
        super().__init_subclass__(*args, **kwargs)
        cls._check_class()

    @classmethod
    def _check_class(cls):
        """
        Verify that :py:meth:`.get_filter` return a callable for all
        policy values and object types.
        """
        mock_module = types.ModuleType('mock_module')

        class MockClass:
            pass

        for member in cls.__members__.values():
            for obj_type in 'func', 'class', 'module':
                for namespace in mock_module, MockClass:
                    assert callable(member.get_filter(namespace, obj_type))

    # Filtering

    def get_filter(self, namespace, obj_type):
        """
        Args:
            namespace (Union[type, types.ModuleType]):
                Class or module to be profiled.
            obj_type (Literal['func', 'class', 'module']):
                Type of object encountered in ``namespace``:

                ``'func'``
                    Either a function, or a component function of a
                    callable-like object (e.g. :py:class:`property`)

                ``'class'`` (resp. ``'module'``)
                    A class (resp. a module)

        Returns:
            func (Callable[..., bool]):
                Filter callable returning whether the argument (as
                specified by ``obj_type``) should be added
                via :py:meth:`LineProfiler.add_class`,
                :py:meth:`LineProfiler.add_module`, or
                :py:meth:`LineProfiler.add_callable`
        """
        is_class = isinstance(namespace, type)
        if obj_type == 'module':
            if is_class:
                return self._return_const(False)
            return self._get_module_filter_in_module(namespace)
        if is_class:
            method = self._get_callable_filter_in_class
        else:
            method = self._get_callable_filter_in_module
        return method(namespace, is_class=(obj_type == 'class'))

    @classmethod
    def to_policies(cls, policies=None):
        """
        Normalize ``policies`` into a dictionary of policies for various
        object types.

        Args:
            policies (Union[str, ScopingPolicy, \
ScopingPolicyDict, None]):
                :py:class:`ScopingPolicy`, string convertible thereto
                (case-insensitive), or a mapping containing such values
                and the keys as outlined in the return value;
                the default :py:const:`None` is equivalent to
                :py:data:`DEFAULT_SCOPING_POLICIES`.

        Returns:
            normalized_policies (dict[Literal['func', 'class', \
'module'], ScopingPolicy]):
                Dictionary with the following key-value pairs:

                ``'func'``
                    :py:class:`ScopingPolicy` for profiling functions
                    and other callable-like objects composed thereof
                    (e.g. :py:class:`property`).

                ``'class'``
                    :py:class:`ScopingPolicy` for descending into
                    classes.

                ``'module'``
                    :py:class:`ScopingPolicy` for descending into
                    modules (if the namespace is itself a module).

        Note:
            If ``policies`` is a mapping, it is required to contain all
            three of the aforementioned keys.

        Example:

            >>> assert (ScopingPolicy.to_policies('children')
            ...         == dict.fromkeys(['func', 'class', 'module'],
            ...                          ScopingPolicy.CHILDREN))
            >>> assert (ScopingPolicy.to_policies({
            ...             'func': 'NONE',
            ...             'class': 'descendants',
            ...             'module': 'exact',
            ...             'unused key': 'unused value'})
            ...         == {'func': ScopingPolicy.NONE,
            ...             'class': ScopingPolicy.DESCENDANTS,
            ...             'module': ScopingPolicy.EXACT})
            >>> ScopingPolicy.to_policies({})
            Traceback (most recent call last):
            ...
            KeyError: 'func'
        """
        if policies is None:
            policies = DEFAULT_SCOPING_POLICIES
        if isinstance(policies, str):
            policy = cls(policies)
            return _ScopingPolicyDict(
                dict.fromkeys(['func', 'class', 'module'], policy))
        return _ScopingPolicyDict({'func': cls(policies['func']),
                                   'class': cls(policies['class']),
                                   'module': cls(policies['module'])})

    @staticmethod
    def _return_const(value):
        def return_const(*_, **__):
            return value

        return return_const

    @staticmethod
    def _match_prefix(s, prefix, sep='.'):
        return s == prefix or s.startswith(prefix + sep)

    def _get_callable_filter_in_class(self, cls, is_class):
        def func_is_child(other):
            if not modules_are_equal(other):
                return False
            return other.__qualname__ == f'{cls.__qualname__}.{other.__name__}'

        def modules_are_equal(other):  # = sibling check
            return cls.__module__ == other.__module__

        def func_is_descdendant(other):
            if not modules_are_equal(other):
                return False
            return other.__qualname__.startswith(cls.__qualname__ + '.')

        return {'exact': (self._return_const(False)
                          if is_class else
                          func_is_child),
                'children': func_is_child,
                'descendants': func_is_descdendant,
                'siblings': modules_are_equal,
                'none': self._return_const(True)}[self.value]

    def _get_callable_filter_in_module(self, mod, is_class):
        def func_is_child(other):
            return other.__module__ == mod.__name__

        def func_is_descdendant(other):
            return self._match_prefix(other.__module__, mod.__name__)

        def func_is_cousin(other):
            if func_is_descdendant(other):
                return True
            return self._match_prefix(other.__module__, parent)

        parent, _, basename = mod.__name__.rpartition('.')
        return {'exact': (self._return_const(False)
                          if is_class else
                          func_is_child),
                'children': func_is_child,
                'descendants': func_is_descdendant,
                'siblings': (func_is_cousin  # Only if a pkg
                             if basename else
                             func_is_descdendant),
                'none': self._return_const(True)}[self.value]

    def _get_module_filter_in_module(self, mod):
        def module_is_descendant(other):
            return other.__name__.startswith(mod.__name__ + '.')

        def module_is_child(other):
            return other.__name__.rpartition('.')[0] == mod.__name__

        def module_is_sibling(other):
            return other.__name__.startswith(parent + '.')

        parent, _, basename = mod.__name__.rpartition('.')
        return {'exact': self._return_const(False),
                'children': module_is_child,
                'descendants': module_is_descendant,
                'siblings': (module_is_sibling  # Only if a pkg
                             if basename else
                             self._return_const(False)),
                'none': self._return_const(True)}[self.value]


# Sanity check in case we extended `ScopingPolicy` and forgot to update
# the corresponding methods
ScopingPolicy._check_class()

ScopingPolicyDict = TypedDict('ScopingPolicyDict',
                              {'func': Union[str, ScopingPolicy],
                               'class': Union[str, ScopingPolicy],
                               'module': Union[str, ScopingPolicy]})
_ScopingPolicyDict = TypedDict('_ScopingPolicyDict',
                               {'func': ScopingPolicy,
                                'class': ScopingPolicy,
                                'module': ScopingPolicy})


class LineProfiler(CLineProfiler, ByCountProfilerMixin):
    """
    A profiler that records the execution times of individual lines.

    This provides the core line-profiler functionality.

    Example:
        >>> import line_profiler
        >>> profile = line_profiler.LineProfiler()
        >>> @profile
        >>> def func():
        >>>     x1 = list(range(10))
        >>>     x2 = list(range(100))
        >>>     x3 = list(range(1000))
        >>> func()
        >>> profile.print_stats()
    """
    def __call__(self, func):
        """
        Decorate a function, method, :py:class:`property`,
        :py:func:`~functools.partial` object etc. to start the profiler
        on function entry and stop it on function exit.
        """
        # The same object is returned when:
        # - `func` is a `types.FunctionType` which is already
        #   decorated by the profiler, or
        # - `func` is any of the C-level callables that can't be
        #   profiled
        # otherwise, wrapper objects are always returned.
        self.add_callable(func)
        return self.wrap_callable(func)

    def wrap_callable(self, func):
        if is_c_level_callable(func):  # Non-profilable
            return func
        return super().wrap_callable(func)

    def add_callable(self, func, guard=None):
        """
        Register a function, method, :py:class:`property`,
        :py:func:`~functools.partial` object, etc. with the underlying
        Cython profiler.

        Args:
            func (...):
                Function, class/static/bound method, property, etc.
            guard (Optional[Callable[[types.FunctionType], bool]])
                Optional checker callable, which takes a function object
                and returns true(-y) if it *should not* be passed to
                :py:meth:`.add_function()`.  Defaults to checking
                whether the function is already a profiling wrapper.

        Returns:
            1 if any function is added to the profiler, 0 otherwise.

        Note:
            This method should in general be called instead of the more
            low-level :py:meth:`.add_function()`.
        """
        if guard is None:
            guard = self._already_a_wrapper

        nadded = 0
        for impl in _get_underlying_functions(func):
            info, wrapped_by_this_prof = self._get_wrapper_info(impl)
            if wrapped_by_this_prof if guard is None else guard(impl):
                continue
            if info:
                # It's still a profiling wrapper, just wrapped by
                # someone else -> extract the inner function
                impl = info.func
            self.add_function(impl)
            nadded += 1

        return 1 if nadded else 0

    def dump_stats(self, filename):
        """ Dump a representation of the data to a file as a pickled
        :py:class:`~.LineStats` object from :py:meth:`~.get_stats()`.
        """
        lstats = self.get_stats()
        with open(filename, 'wb') as f:
            pickle.dump(lstats, f, pickle.HIGHEST_PROTOCOL)

    def print_stats(self, stream=None, output_unit=None, stripzeros=False,
                    details=True, summarize=False, sort=False, rich=False):
        """ Show the gathered statistics.
        """
        lstats = self.get_stats()
        show_text(lstats.timings, lstats.unit, output_unit=output_unit,
                  stream=stream, stripzeros=stripzeros,
                  details=details, summarize=summarize, sort=sort, rich=rich)

    def _add_namespace(
            self, namespace, *,
            seen=None,
            func_scoping_policy=ScopingPolicy.NONE,
            class_scoping_policy=ScopingPolicy.NONE,
            module_scoping_policy=ScopingPolicy.NONE,
            wrap=False):
        def func_guard(func):
            return self._already_a_wrapper(func) or not func_check(func)

        if seen is None:
            seen = set()
        count = 0
        add_namespace = functools.partial(
            self._add_namespace,
            seen=seen,
            func_scoping_policy=func_scoping_policy,
            class_scoping_policy=class_scoping_policy,
            module_scoping_policy=module_scoping_policy,
            wrap=wrap)
        wrap_failures = {}
        func_check = func_scoping_policy.get_filter(namespace, 'func')
        cls_check = class_scoping_policy.get_filter(namespace, 'class')
        mod_check = module_scoping_policy.get_filter(namespace, 'module')

        for attr, value in vars(namespace).items():
            if id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, type):
                if cls_check(value) and add_namespace(value):
                    count += 1
                continue
            elif isinstance(value, types.ModuleType):
                if mod_check(value) and add_namespace(value):
                    count += 1
                continue
            try:
                if not self.add_callable(value, guard=func_guard):
                    continue
            except TypeError:  # Not a callable (wrapper)
                continue
            if wrap:
                wrapper = self.wrap_callable(value)
                if wrapper is not value:
                    try:
                        setattr(namespace, attr, wrapper)
                    except (TypeError, AttributeError):
                        # Corner case in case if a class/module don't
                        # allow setting attributes (could e.g. happen
                        # with some builtin/extension classes, but their
                        # method should be in C anyway, so
                        # `.add_callable()` should've returned 0 and we
                        # shouldn't be here)
                        wrap_failures[attr] = value
            count += 1
        if wrap_failures:
            msg = (f'cannot wrap {len(wrap_failures)} attribute(s) of '
                   f'{namespace!r} (`{{attr: value}}`): {wrap_failures!r}')
            warnings.warn(msg, stacklevel=2)
        return count

    def add_class(self, cls, *, scoping_policy=None, wrap=False):
        """
        Add the members (callables (wrappers), methods, classes, ...) in
        a class' local namespace and profile them.

        Args:
            cls (type):
                Class to be profiled.
            scoping_policy (Union[str, ScopingPolicy, \
ScopingPolicyDict, None]):
                Whether (and how) to match the scope of members and
                decide on whether to add them:

                :py:class:`str` (incl. :py:class:`ScopingPolicy`):
                    Strings are converted to :py:class:`ScopingPolicy`
                    instances in a case-insensitive manner, and the same
                    policy applies to all members.

                ``{'func': ..., 'class': ..., 'module': ...}``
                    Mapping specifying individual policies to be enacted
                    for the corresponding member types.

                :py:const:`None`
                    The default, equivalent to
                    :py:data:`DEFAULT_SCOPING_POLICIES`.

                See :py:class:`ScopingPolicy` and
                :py:meth:`~ScopingPolicy.to_policies` for details.
            wrap (bool):
                Whether to replace the wrapped members with wrappers
                which automatically enable/disable the profiler when
                called.

        Returns:
            n (int):
                Number of members added to the profiler.
        """
        policies = ScopingPolicy.to_policies(scoping_policy)
        return self._add_namespace(cls,
                                   func_scoping_policy=policies['func'],
                                   class_scoping_policy=policies['class'],
                                   module_scoping_policy=policies['module'],
                                   wrap=wrap)

    def add_module(self, mod, *, scoping_policy=None, wrap=False):
        """
        Add the members (callables (wrappers), methods, classes, ...) in
        a module's local namespace and profile them.

        Args:
            mod (ModuleType):
                Module to be profiled.
            scoping_policy (Union[str, ScopingPolicy, \
ScopingPolicyDict, None]):
                Whether (and how) to match the scope of members and
                decide on whether to add them:

                :py:class:`str` (incl. :py:class:`ScopingPolicy`):
                    Strings are converted to :py:class:`ScopingPolicy`
                    instances in a case-insensitive manner, and the same
                    policy applies to all members.

                ``{'func': ..., 'class': ..., 'module': ...}``
                    Mapping specifying individual policies to be enacted
                    for the corresponding member types.

                :py:const:`None`
                    The default, equivalent to
                    :py:data:`DEFAULT_SCOPING_POLICIES`.

                See :py:class:`ScopingPolicy` and
                :py:meth:`~ScopingPolicy.to_policies` for details.
            wrap (bool):
                Whether to replace the wrapped members with wrappers
                which automatically enable/disable the profiler when
                called.

        Returns:
            n (int):
                Number of members added to the profiler.
        """
        policies = ScopingPolicy.to_policies(scoping_policy)
        return self._add_namespace(mod,
                                   func_scoping_policy=policies['func'],
                                   class_scoping_policy=policies['class'],
                                   module_scoping_policy=policies['module'],
                                   wrap=wrap)

    def _get_wrapper_info(self, func):
        info = getattr(func, self._profiler_wrapped_marker, None)
        return info, bool(info and id(self) == info.profiler_id)

    # Override these mixed-in bookkeeping methods to take care of
    # potential multiple profiler sequences

    def _already_a_wrapper(self, func):
        return self._get_wrapper_info(func)[1]

    def _mark_wrapper(self, wrapper):
        # Are re-wrapping an existing wrapper (e.g. created by another
        # profiler?)
        wrapped = wrapper.__wrapped__
        info = getattr(wrapped, self._profiler_wrapped_marker, None)
        new_info = _WrapperInfo(info.func if info else wrapped, id(self))
        setattr(wrapper, self._profiler_wrapped_marker, new_info)
        return wrapper


# This could be in the ipython_extension submodule,
# but it doesn't depend on the IPython module so it's easier to just let it stay here.
def is_generated_code(filename):
    """ Return True if a filename corresponds to generated code, such as a
    Jupyter Notebook cell.
    """
    filename = os.path.normcase(filename)
    temp_dir = os.path.normcase(tempfile.gettempdir())
    return (
        filename.startswith('<generated') or
        filename.startswith('<ipython-input-') or
        filename.startswith(os.path.join(temp_dir, 'ipykernel_')) or
        filename.startswith(os.path.join(temp_dir, 'xpython_'))
    )


def show_func(filename, start_lineno, func_name, timings, unit,
              output_unit=None, stream=None, stripzeros=False, rich=False):
    """
    Show results for a single function.

    Args:
        filename (str):
            path to the profiled file

        start_lineno (int):
            first line number of profiled function

        func_name (str): name of profiled function

        timings (List[Tuple[int, int, float]]):
            measurements for each line (lineno, nhits, time).

        unit (float):
            The number of seconds used as the cython LineProfiler's unit.

        output_unit (float | None):
            Output unit (in seconds) in which the timing info is displayed.

        stream (io.TextIOBase | None):
            defaults to sys.stdout

        stripzeros (bool):
            if True, prints nothing if the function was not run

        rich (bool):
            if True, attempt to use rich highlighting.

    Example:
        >>> from line_profiler.line_profiler import show_func
        >>> import line_profiler
        >>> # Use a function in this file as an example
        >>> func = line_profiler.line_profiler.show_text
        >>> start_lineno = func.__code__.co_firstlineno
        >>> filename = func.__code__.co_filename
        >>> func_name = func.__name__
        >>> # Build fake timeings for each line in the example function
        >>> import inspect
        >>> num_lines = len(inspect.getsourcelines(func)[0])
        >>> line_numbers = list(range(start_lineno + 3, start_lineno + num_lines))
        >>> timings = [
        >>>     (lineno, idx * 1e13, idx * (2e10 ** (idx % 3)))
        >>>     for idx, lineno in enumerate(line_numbers, start=1)
        >>> ]
        >>> unit = 1.0
        >>> output_unit = 1.0
        >>> stream = None
        >>> stripzeros = False
        >>> rich = 1
        >>> show_func(filename, start_lineno, func_name, timings, unit,
        >>>           output_unit, stream, stripzeros, rich)
    """
    if stream is None:
        stream = sys.stdout

    total_hits = sum(t[1] for t in timings)
    total_time = sum(t[2] for t in timings)

    if stripzeros and total_hits == 0:
        return

    if rich:
        # References:
        # https://github.com/Textualize/rich/discussions/3076
        try:
            from rich.syntax import Syntax
            from rich.highlighter import ReprHighlighter
            from rich.text import Text
            from rich.console import Console
            from rich.table import Table
        except ImportError:
            rich = 0

    if output_unit is None:
        output_unit = unit
    scalar = unit / output_unit

    linenos = [t[0] for t in timings]

    stream.write('Total time: %g s\n' % (total_time * unit))
    if os.path.exists(filename) or is_generated_code(filename):
        stream.write(f'File: {filename}\n')
        stream.write(f'Function: {func_name} at line {start_lineno}\n')
        if os.path.exists(filename):
            # Clear the cache to ensure that we get up-to-date results.
            linecache.clearcache()
        all_lines = linecache.getlines(filename)
        sublines = inspect.getblock(all_lines[start_lineno - 1:])
    else:
        stream.write('\n')
        stream.write(f'Could not find file {filename}\n')
        stream.write('Are you sure you are running this program from the same directory\n')
        stream.write('that you ran the profiler from?\n')
        stream.write("Continuing without the function's contents.\n")
        # Fake empty lines so we can see the timings, if not the code.
        nlines = 1 if not linenos else max(linenos) - min(min(linenos), start_lineno) + 1
        sublines = [''] * nlines

    # Define minimum column sizes so text fits and usually looks consistent
    default_column_sizes = {
        'line': 6,
        'hits': 9,
        'time': 12,
        'perhit': 8,
        'percent': 8,
    }

    display = {}

    # Loop over each line to determine better column formatting.
    # Fallback to scientific notation if columns are larger than a threshold.
    for lineno, nhits, time in timings:
        if total_time == 0:  # Happens rarely on empty function
            percent = ''
        else:
            percent = '%5.1f' % (100 * time / total_time)

        time_disp = '%5.1f' % (time * scalar)
        if len(time_disp) > default_column_sizes['time']:
            time_disp = '%5.3g' % (time * scalar)

        perhit_disp = '%5.1f' % (float(time) * scalar / nhits)
        if len(perhit_disp) > default_column_sizes['perhit']:
            perhit_disp = '%5.3g' % (float(time) * scalar / nhits)

        nhits_disp = "%d" % nhits
        if len(nhits_disp) > default_column_sizes['hits']:
            nhits_disp = '%g' % nhits

        display[lineno] = (nhits_disp, time_disp, perhit_disp, percent)

    # Expand column sizes if the numbers are large.
    column_sizes = default_column_sizes.copy()
    if len(display):
        max_hitlen = max(len(t[0]) for t in display.values())
        max_timelen = max(len(t[1]) for t in display.values())
        max_perhitlen = max(len(t[2]) for t in display.values())
        column_sizes['hits'] = max(column_sizes['hits'], max_hitlen)
        column_sizes['time'] = max(column_sizes['time'], max_timelen)
        column_sizes['perhit'] = max(column_sizes['perhit'], max_perhitlen)

    col_order = ['line', 'hits', 'time', 'perhit', 'percent']
    lhs_template = ' '.join(['%' + str(column_sizes[k]) + 's' for k in col_order])
    template = lhs_template + '  %-s'

    linenos = range(start_lineno, start_lineno + len(sublines))
    empty = ('', '', '', '')
    header = ('Line #', 'Hits', 'Time', 'Per Hit', '% Time', 'Line Contents')
    header = template % header
    stream.write('\n')
    stream.write(header)
    stream.write('\n')
    stream.write('=' * len(header))
    stream.write('\n')

    if rich:
        # Build the RHS and LHS of the table separately
        lhs_lines = []
        rhs_lines = []
        for lineno, line in zip(linenos, sublines):
            nhits, time, per_hit, percent = display.get(lineno, empty)
            txt = lhs_template % (lineno, nhits, time, per_hit, percent)
            rhs_lines.append(line.rstrip('\n').rstrip('\r'))
            lhs_lines.append(txt)

        rhs_text = '\n'.join(rhs_lines)
        lhs_text = '\n'.join(lhs_lines)

        # Highlight the RHS with Python syntax
        rhs = Syntax(rhs_text, 'python', background_color='default')

        # Use default highlights for the LHS
        # TODO: could use colors to draw the eye to longer running lines.
        lhs = Text(lhs_text)
        ReprHighlighter().highlight(lhs)

        # Use a table to horizontally concatenate the text
        # reference: https://github.com/Textualize/rich/discussions/3076
        table = Table(
            box=None,
            padding=0,
            collapse_padding=True,
            show_header=False,
            show_footer=False,
            show_edge=False,
            pad_edge=False,
            expand=False,
        )
        table.add_row(lhs, '  ', rhs)

        # Use a Console to render to the stream
        # Not sure if we should force-terminal or just specify the color system
        # write_console = Console(file=stream, force_terminal=True, soft_wrap=True)
        write_console = Console(file=stream, soft_wrap=True, color_system='standard')
        write_console.print(table)
        stream.write('\n')
    else:
        for lineno, line in zip(linenos, sublines):
            nhits, time, per_hit, percent = display.get(lineno, empty)
            line_ = line.rstrip('\n').rstrip('\r')
            txt = template % (lineno, nhits, time, per_hit, percent, line_)
            try:
                stream.write(txt)
            except UnicodeEncodeError:
                # todo: better handling of windows encoding issue
                # for now just work around it
                line_ = 'UnicodeEncodeError - help wanted for a fix'
                txt = template % (lineno, nhits, time, per_hit, percent, line_)
                stream.write(txt)

            stream.write('\n')
    stream.write('\n')


def show_text(stats, unit, output_unit=None, stream=None, stripzeros=False,
              details=True, summarize=False, sort=False, rich=False):
    """
    Show text for the given timings.

    Ignore:
        # For developer testing, generate some profile output
        python -m kernprof -l -p uuid -m uuid

        # Use this function to view it with rich
        python -m line_profiler -rmtz "uuid.lprof"

        # Use this function to view it without rich
        python -m line_profiler -mtz "uuid.lprof"
    """
    if stream is None:
        stream = sys.stdout

    if output_unit is not None:
        stream.write('Timer unit: %g s\n\n' % output_unit)
    else:
        stream.write('Timer unit: %g s\n\n' % unit)

    if sort:
        # Order by ascending duration
        stats_order = sorted(stats.items(), key=lambda kv: sum(t[2] for t in kv[1]))
    else:
        # Default ordering
        stats_order = sorted(stats.items())

    if details:
        # Show detailed per-line information for each function.
        for (fn, lineno, name), timings in stats_order:
            show_func(fn, lineno, name, stats[fn, lineno, name], unit,
                      output_unit=output_unit, stream=stream,
                      stripzeros=stripzeros, rich=rich)

    if summarize:
        # Summarize the total time for each function
        if rich:
            try:
                from rich.console import Console
                from rich.markup import escape
            except ImportError:
                rich = 0
        line_template = '%6.2f seconds - %s:%s - %s'
        if rich:
            write_console = Console(file=stream, soft_wrap=True,
                                    color_system='standard')
            for (fn, lineno, name), timings in stats_order:
                total_time = sum(t[2] for t in timings) * unit
                if not stripzeros or total_time:
                    # Wrap the filename with link markup to allow the user to
                    # open the file
                    fn_link = f'[link={fn}]{escape(fn)}[/link]'
                    line = line_template % (total_time, fn_link, lineno, escape(name))
                    write_console.print(line)
        else:
            for (fn, lineno, name), timings in stats_order:
                total_time = sum(t[2] for t in timings) * unit
                if not stripzeros or total_time:
                    line = line_template % (total_time, fn, lineno, name)
                    stream.write(line + '\n')


def load_stats(filename):
    """ Utility function to load a pickled :py:class:`~.LineStats`
    object from a given filename.
    """
    with open(filename, 'rb') as f:
        return pickle.load(f)


def main():
    """
    The line profiler CLI to view output from :command:`kernprof -l`.
    """
    def positive_float(value):
        val = float(value)
        if val <= 0:
            raise ArgumentError
        return val

    parser = ArgumentParser()
    parser.add_argument('-V', '--version', action='version', version=__version__)
    parser.add_argument(
        '-u',
        '--unit',
        default='1e-6',
        type=positive_float,
        help='Output unit (in seconds) in which the timing info is displayed (default: 1e-6)',
    )
    parser.add_argument(
        '-z',
        '--skip-zero',
        action='store_true',
        help='Hide functions which have not been called',
    )
    parser.add_argument(
        '-r',
        '--rich',
        action='store_true',
        help='Use rich formatting',
    )
    parser.add_argument(
        '-t',
        '--sort',
        action='store_true',
        help='Sort by ascending total time',
    )
    parser.add_argument(
        '-m',
        '--summarize',
        action='store_true',
        help='Print a summary of total function time',
    )
    parser.add_argument('profile_output', help='*.lprof file created by kernprof')

    args = parser.parse_args()
    lstats = load_stats(args.profile_output)
    show_text(
        lstats.timings, lstats.unit, output_unit=args.unit,
        stripzeros=args.skip_zero,
        rich=args.rich,
        sort=args.sort,
        summarize=args.summarize,
    )


if __name__ == '__main__':
    main()
