#!/usr/bin/env python
"""
This module defines the core :class:`LineProfiler` class as well as methods to
inspect its output. This depends on the :py:mod:`line_profiler._line_profiler`
Cython backend.
"""
import functools
import inspect
import linecache
import operator
import os
import pickle
import sys
import tempfile
import types
import tokenize
from argparse import ArgumentParser
from datetime import datetime

try:
    from ._line_profiler import (LineProfiler as CLineProfiler,
                                 LineStats as CLineStats)
except ImportError as ex:
    raise ImportError(
        'The line_profiler._line_profiler c-extension is not importable. '
        f'Has it been compiled? Underlying error is ex={ex!r}'
    )
from . import _diagnostics as diagnostics
from .cli_utils import (
    add_argument, get_cli_config, positive_float, short_string_path)
from .profiler_mixin import ByCountProfilerMixin, is_c_level_callable
from .scoping_policy import ScopingPolicy
from .toml_config import ConfigSource


# NOTE: This needs to be in sync with ../kernprof.py and __init__.py
__version__ = '5.0.1'


@functools.lru_cache()
def get_column_widths(config=False):
    """
    Arguments
        config (bool | str | pathlib.PurePath | None)
            Passed to :py:meth:`.ConfigSource.from_config`.
    Note:
        * Results are cached.
        * The default value (:py:data:`False`) loads the config from the
          default TOML file that the package ships with.
    """
    subconf = (ConfigSource.from_config(config)
               .get_subconfig('show', 'column_widths'))
    return types.MappingProxyType(subconf.conf_dict)


def load_ipython_extension(ip):
    """ API for IPython to recognize this module as an IPython extension.
    """
    from .ipython_extension import LineProfilerMagics
    ip.register_magics(LineProfilerMagics)


def get_code_block(filename, lineno):
    """
    Get the lines in the code block in a file starting from required
    line number; understands Cython code.

    Args:
        filename (Union[os.PathLike, str])
            Path to the source file.
        lineno (int)
            1-indexed line number of the first line in the block.

    Returns:
        lines (list[str])
            Newline-terminated string lines.

    Note:
        This function makes use of :py:func:`inspect.getblock`, which is
        public but undocumented API.  That said, it has been in use in
        this repo since 2008 (`fb60664`_), so we will continue using it
        until we can't.

        .. _fb60664: https://github.com/pyutils/line_profiler/commit/\
fb60664135296ba6061cfaa2bb66d4ba77964c53


    Example:
        >>> from os.path import join
        >>> from tempfile import TemporaryDirectory
        >>> from textwrap import dedent
        >>>
        >>>
        >>> def get_last_line(*args, **kwargs):
        ...     lines = get_code_block(*args, **kwargs)
        ...     return lines[-1].rstrip('\\n')
        ...
        >>>
        >>> with TemporaryDirectory() as tmpdir:
        ...     fname = join(tmpdir, 'cython_source.pyx')
        ...     with open(fname, mode='w') as fobj:
        ...         print(dedent('''
        ...     class NormalClass:                   # 1
        ...         def __init__(self):              # 2
        ...             pass                         # 3
        ...
        ...         def normal_method(self, *args):  # 5
        ...             pass                         # 6
        ...
        ...     cdef class CythonClass:              # 8
        ...         cpdef cython_method(self):       # 9
        ...             pass                         # 10
        ...
        ...         property legacy_cython_prop:     # 12
        ...             def __get__(self):           # 13
        ...                 return None              # 14
        ...             def __set__(self, value):    # 15
        ...                 pass                     # 16
        ...
        ...     def normal_func(x, y, z):            # 18
        ...         with some_ctx():                 # 19
        ...             ...                          # 20
        ...
        ...     cdef cython_function(                # 22
        ...             int x, int y, int z):        # 23
        ...         ...                              # 24
        ...                      ''').strip('\\n'),
        ...               file=fobj)
        ...     # Vanilla Python code blocks:
        ...     # - `NormalClass`
        ...     assert get_last_line(fname, 1).endswith('# 6')
        ...     # - `NormalClass.__init__()`
        ...     assert get_last_line(fname, 2).endswith('# 3')
        ...     # - `normal_func()`
        ...     assert get_last_line(fname, 18).endswith('# 20')
        ...     # Cython code blocks:
        ...     # - `CythonClass`
        ...     assert get_last_line(fname, 8).endswith('# 16')
        ...     # - `CythonClass.cython_method()`
        ...     assert get_last_line(fname, 9).endswith('# 10')
        ...     # - `CythonClass.legacy_cython_prop`
        ...     assert get_last_line(fname, 12).endswith('# 16')
        ...     # - `cython_function()`
        ...     assert get_last_line(fname, 22).endswith('# 24')
    """
    BlockFinder = inspect.BlockFinder
    namespace = inspect.getblock.__globals__
    namespace['BlockFinder'] = _CythonBlockFinder
    try:
        return inspect.getblock(linecache.getlines(filename)[lineno - 1:])
    finally:
        namespace['BlockFinder'] = BlockFinder


class _CythonBlockFinder(inspect.BlockFinder):
    """
    Compatibility layer turning Cython-specific code blocks (``cdef``,
    ``cpdef``, and legacy ``property`` declaration) into something that
    is understood by :py:class:`inspect.BlockFinder`.

    Note:
        This function makes use of :py:func:`inspect.BlockFinder`, which
        is public but undocumented API.  See similar caveat in
        :py:func:`~.get_code_block`.
    """
    def tokeneater(self, type, token, *args, **kwargs):
        if (
                not self.started
                and type == tokenize.NAME
                and token in ('cdef', 'cpdef', 'property')):
            # Fudge the token to get the desired 'scoping' behavior
            token = 'def'
        return super().tokeneater(type, token, *args, **kwargs)


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


class LineStats(CLineStats):
    def __repr__(self):
        return '{}({}, {:.2G})'.format(
            type(self).__name__, self.timings, self.unit)

    def __eq__(self, other):
        """
        Example:
            >>> from copy import deepcopy
            >>>
            >>>
            >>> stats1 = LineStats(
            ...     {('foo', 1, 'spam.py'): [(2, 10, 300)],
            ...      ('bar', 10, 'spam.py'):
            ...      [(11, 2, 1000), (12, 1, 500)]},
            ...     1E-6)
            >>> stats2 = deepcopy(stats1)
            >>> assert stats1 == stats2 is not stats1
            >>> stats2.timings = 1E-7
            >>> assert stats2 != stats1
            >>> stats3 = deepcopy(stats1)
            >>> assert stats1 == stats3 is not stats1
            >>> stats3.timings['foo', 1, 'spam.py'][:] = [(2, 11, 330)]
            >>> assert stats3 != stats1
        """
        for attr in 'timings', 'unit':
            getter = operator.attrgetter(attr)
            try:
                if getter(self) != getter(other):
                    return False
            except (AttributeError, TypeError):
                return NotImplemented
        return True

    def __add__(self, other):
        """
        Example:
            >>> stats1 = LineStats(
            ...     {('foo', 1, 'spam.py'): [(2, 10, 300)],
            ...      ('bar', 10, 'spam.py'):
            ...      [(11, 2, 1000), (12, 1, 500)]},
            ...     1E-6)
            >>> stats2 = LineStats(
            ...     {('bar', 10, 'spam.py'):
            ...      [(11, 10, 20000), (12, 5, 1000)],
            ...      ('baz', 5, 'eggs.py'): [(5, 2, 5000)]},
            ...     1E-7)
            >>> stats_sum = LineStats(
            ...     {('foo', 1, 'spam.py'): [(2, 10, 300)],
            ...      ('bar', 10, 'spam.py'):
            ...      [(11, 12, 3000), (12, 6, 600)],
            ...      ('baz', 5, 'eggs.py'): [(5, 2, 500)]},
            ...     1E-6)
            >>> assert stats1 + stats2 == stats2 + stats1 == stats_sum
        """
        timings, unit = self._get_aggregated_timings([self, other])
        return type(self)(timings, unit)

    def __iadd__(self, other):
        """
        Example:
            >>> stats1 = LineStats(
            ...     {('foo', 1, 'spam.py'): [(2, 10, 300)],
            ...      ('bar', 10, 'spam.py'):
            ...      [(11, 2, 1000), (12, 1, 500)]},
            ...     1E-6)
            >>> stats2 = LineStats(
            ...     {('bar', 10, 'spam.py'):
            ...      [(11, 10, 20000), (12, 5, 1000)],
            ...      ('baz', 5, 'eggs.py'): [(5, 2, 5000)]},
            ...     1E-7)
            >>> stats_sum = LineStats(
            ...     {('foo', 1, 'spam.py'): [(2, 10, 300)],
            ...      ('bar', 10, 'spam.py'):
            ...      [(11, 12, 3000), (12, 6, 600)],
            ...      ('baz', 5, 'eggs.py'): [(5, 2, 500)]},
            ...     1E-6)
            >>> address = id(stats2)
            >>> stats2 += stats1
            >>> assert id(stats2) == address
            >>> assert stats2 == stats_sum
        """
        self.timings, self.unit = self._get_aggregated_timings([self, other])
        return self

    def print(self, stream=None, **kwargs):
        show_text(self.timings, self.unit, stream=stream, **kwargs)

    def to_file(self, filename):
        """ Pickle the instance to the given filename.
        """
        with open(filename, 'wb') as f:
            pickle.dump(self, f, pickle.HIGHEST_PROTOCOL)

    @classmethod
    def from_files(cls, file, /, *files):
        """
        Utility function to load an instance from the given filenames.
        """
        stats_objs = []
        for file in [file, *files]:
            with open(file, 'rb') as f:
                stats_objs.append(pickle.load(f))
        return cls.from_stats_objects(*stats_objs)

    @classmethod
    def from_stats_objects(cls, stats, /, *more_stats):
        """
        Example:
            >>> stats1 = LineStats(
            ...     {('foo', 1, 'spam.py'): [(2, 10, 300)],
            ...      ('bar', 10, 'spam.py'):
            ...      [(11, 2, 1000), (12, 1, 500)]},
            ...     1E-6)
            >>> stats2 = LineStats(
            ...     {('bar', 10, 'spam.py'):
            ...      [(11, 10, 20000), (12, 5, 1000)],
            ...      ('baz', 5, 'eggs.py'): [(5, 2, 5000)]},
            ...     1E-7)
            >>> stats_combined = LineStats.from_stats_objects(
            ...     stats1, stats2)
            >>> assert stats_combined.unit == 1E-6
            >>> assert stats_combined.timings == {
            ...     ('foo', 1, 'spam.py'): [(2, 10, 300)],
            ...     ('bar', 10, 'spam.py'):
            ...     [(11, 12, 3000), (12, 6, 600)],
            ...     ('baz', 5, 'eggs.py'): [(5, 2, 500)]}
        """
        timings, unit = cls._get_aggregated_timings([stats, *more_stats])
        return cls(timings, unit)

    @staticmethod
    def _get_aggregated_timings(stats_objs):
        if not stats_objs:
            raise ValueError(f'stats_objs = {stats_objs!r}: empty')
        try:
            stats, = stats_objs
        except ValueError:  # > 1 obj
            # Add from small scaling factors to large to minimize
            # rounding errors
            stats_objs = sorted(stats_objs, key=operator.attrgetter('unit'))
            unit = stats_objs[-1].unit
            # type: dict[tuple[str, int, int], dict[int, tuple[int, float]]
            timing_dict = {}
            for stats in stats_objs:
                factor = stats.unit / unit
                for key, entries in stats.timings.items():
                    entry_dict = timing_dict.setdefault(key, {})
                    for lineno, nhits, time in entries:
                        prev_nhits, prev_time = entry_dict.get(lineno, (0, 0))
                        entry_dict[lineno] = (prev_nhits + nhits,
                                              prev_time + factor * time)
            timings = {
                key: [(lineno, nhits, int(round(time, 0)))
                      for lineno, (nhits, time) in sorted(entry_dict.items())]
                for key, entry_dict in timing_dict.items()}
        else:
            timings = {key: entries.copy()
                       for key, entries in stats.timings.items()}
            unit = stats.unit
        return timings, unit


class LineProfiler(CLineProfiler, ByCountProfilerMixin):
    """
    A profiler that records the execution times of individual lines.

    This provides the core line-profiler functionality.

    Example:
        >>> import line_profiler
        >>> profile = line_profiler.LineProfiler()
        >>> @profile
        ... def func():
        ...     x1 = list(range(10))
        ...     x2 = list(range(100))
        ...     x3 = list(range(1000))
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
        #   decorated by the profiler,
        # - `func` is a class, or
        # - `func` is any of the C-level callables that can't be
        #   profiled
        # otherwise, wrapper objects are always returned.
        self.add_callable(func)
        return self.wrap_callable(func)

    def wrap_callable(self, func):
        if is_c_level_callable(func):  # Non-profilable
            return func
        return super().wrap_callable(func)

    def add_callable(self, func, guard=None, name=None):
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
            name (Optional[str])
                Optional name for ``func``, to be used in log messages.

        Returns:
            1 if any function is added to the profiler, 0 otherwise.

        Note:
            This method should in general be called instead of the more
            low-level :py:meth:`.add_function()`.
        """
        if guard is None:
            guard = self._already_a_wrapper

        nadded = 0
        func_repr = self._repr_for_log(func, name)
        for impl in self.get_underlying_functions(func):
            info, wrapped_by_this_prof = self._get_wrapper_info(impl)
            if wrapped_by_this_prof if guard is None else guard(impl):
                continue
            if info:
                # It's still a profiling wrapper, just wrapped by
                # someone else -> extract the inner function
                impl = info.func
            self.add_function(impl)
            nadded += 1
            if impl is func:
                self._debug(f'added {func_repr}')
            else:
                self._debug(f'added {func_repr} -> {self._repr_for_log(impl)}')

        return 1 if nadded else 0

    @staticmethod
    def _repr_for_log(obj, name=None):
        try:
            real_name = '{0.__module__}.{0.__qualname__}'.format(obj)
        except AttributeError:
            try:
                real_name = obj.__name__
            except AttributeError:
                real_name = '???'
        return '{} `{}{}` {}@ {:#x}'.format(
            type(obj).__name__,
            real_name,
            '()' if callable(obj) and not isinstance(obj, type) else '',
            f'(=`{name}`) ' if name and name != real_name else '',
            id(obj))

    def _debug(self, msg):
        self_repr = f'{type(self).__name__} @ {id(self):#x}'
        logger = diagnostics.log
        if logger.backend == 'print':
            now = datetime.now().isoformat(sep=' ', timespec='seconds')
            msg = f'[{self_repr} {now}] {msg}'
        else:
            msg = f'{self_repr}: {msg}'
        logger.debug(msg)

    def get_stats(self):
        return LineStats.from_stats_objects(super().get_stats())

    def dump_stats(self, filename):
        """ Dump a representation of the data to a file as a pickled
        :py:class:`~.LineStats` object from :py:meth:`~.get_stats()`.
        """
        self.get_stats().to_file(filename)

    def print_stats(self, stream=None, output_unit=None, stripzeros=False,
                    details=True, summarize=False, sort=False, rich=False, *,
                    config=None):
        """ Show the gathered statistics.
        """
        self.get_stats().print(
            stream=stream, output_unit=output_unit,
            stripzeros=stripzeros, details=details, summarize=summarize,
            sort=sort, rich=rich, config=config)

    def _add_namespace(
            self, namespace, *,
            seen=None,
            func_scoping_policy=ScopingPolicy.NONE,
            class_scoping_policy=ScopingPolicy.NONE,
            module_scoping_policy=ScopingPolicy.NONE,
            wrap=False,
            name=None):
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
        members_to_wrap = {}
        func_check = func_scoping_policy.get_filter(namespace, 'func')
        cls_check = class_scoping_policy.get_filter(namespace, 'class')
        mod_check = module_scoping_policy.get_filter(namespace, 'module')

        # Logging stuff
        if not name:
            try:  # Class
                name = '{0.__module__}.{0.__qualname__}'.format(namespace)
            except AttributeError:  # Module
                name = namespace.__name__

        for attr, value in vars(namespace).items():
            if id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, type):
                if not (cls_check(value)
                        and add_namespace(value, name=f'{name}.{attr}')):
                    continue
            elif isinstance(value, types.ModuleType):
                if not (mod_check(value)
                        and add_namespace(value, name=f'{name}.{attr}')):
                    continue
            else:
                try:
                    if not self.add_callable(
                            value, guard=func_guard, name=f'{name}.{attr}'):
                        continue
                except TypeError:  # Not a callable (wrapper)
                    continue
                if wrap:
                    members_to_wrap[attr] = value
            count += 1
        if wrap and members_to_wrap:
            self._wrap_namespace_members(namespace, members_to_wrap,
                                         warning_stack_level=3)
        if count:
            self._debug(
                'added {} member{} in {}'.format(
                    count,
                    '' if count == 1 else 's',
                    self._repr_for_log(namespace, name)))
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

                :py:class:`str` (incl. :py:class:`~.ScopingPolicy`):
                    Strings are converted to :py:class:`~.ScopingPolicy`
                    instances in a case-insensitive manner, and the same
                    policy applies to all members.

                ``{'func': ..., 'class': ..., 'module': ...}``
                    Mapping specifying individual policies to be enacted
                    for the corresponding member types.

                :py:const:`None`
                    The default, equivalent to
                    :py:data:\
`~.scoping_policy.DEFAULT_SCOPING_POLICIES`.

                See :py:class:`~.ScopingPolicy` and
                :py:meth:`.ScopingPolicy.to_policies` for details.
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

                :py:class:`str` (incl. :py:class:`~.ScopingPolicy`):
                    Strings are converted to :py:class:`~.ScopingPolicy`
                    instances in a case-insensitive manner, and the same
                    policy applies to all members.

                ``{'func': ..., 'class': ..., 'module': ...}``
                    Mapping specifying individual policies to be enacted
                    for the corresponding member types.

                :py:const:`None`
                    The default, equivalent to
                    :py:data:\
`~.scoping_policy.DEFAULT_SCOPING_POLICIES`.

                See :py:class:`~.ScopingPolicy` and
                :py:meth:`.ScopingPolicy.to_policies` for details.
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
              output_unit=None, stream=None, stripzeros=False, rich=False,
              *,
              config=None):
    """
    Show results for a single function.

    Args:
        filename (str):
            Path to the profiled file

        start_lineno (int):
            First line number of profiled function

        func_name (str): name of profiled function

        timings (List[Tuple[int, int, float]]):
            Measurements for each line (lineno, nhits, time).

        unit (float):
            The number of seconds used as the cython LineProfiler's unit.

        output_unit (float | None):
            Output unit (in seconds) in which the timing info is displayed.

        stream (io.TextIOBase | None):
            Defaults to sys.stdout

        stripzeros (bool):
            If True, prints nothing if the function was not run

        rich (bool):
            If True, attempt to use rich highlighting.

        config (Union[str, PurePath, bool, None]):
            Optional filename from which to load configurations (e.g.
            output column widths);
            default (= `True` or `None`) is to look for a config file
            based on the environment variable `${LINE_PROFILER_RC}` and
            path-based lookup;
            passing `False` disables all lookup and falls back to the
            default configuration

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
        >>> line_numbers = list(range(start_lineno + 3,
        ...                           start_lineno + num_lines))
        >>> timings = [(lineno, idx * 1e13, idx * (2e10 ** (idx % 3)))
        ...            for idx, lineno
        ...            in enumerate(line_numbers, start=1)]
        >>> unit = 1.0
        >>> output_unit = 1.0
        >>> stream = None
        >>> stripzeros = False
        >>> rich = 1
        >>> show_func(filename, start_lineno, func_name, timings, unit,
        ...           output_unit, stream, stripzeros, rich)
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
        sublines = get_code_block(filename, start_lineno)
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
    conf_column_sizes = get_column_widths(config)
    default_column_sizes = {
        col: max(width, conf_column_sizes.get(col, width))
        for col, width in get_column_widths().items()}

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
        table = Table(box=None,
                      padding=0,
                      collapse_padding=True,
                      show_header=False,
                      show_footer=False,
                      show_edge=False,
                      pad_edge=False,
                      expand=False)
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
              details=True, summarize=False, sort=False, rich=False, *,
              config=None):
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
        stats_order = stats.items()

    # Pre-lookup the appropriate config file
    config = ConfigSource.from_config(config).path

    if details:
        # Show detailed per-line information for each function.
        for (fn, lineno, name), timings in stats_order:
            show_func(fn, lineno, name, stats[fn, lineno, name], unit,
                      output_unit=output_unit, stream=stream,
                      stripzeros=stripzeros, rich=rich, config=config)

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


load_stats = LineStats.from_files


def main():
    """
    The line profiler CLI to view output from :command:`kernprof -l`.
    """
    parser = ArgumentParser(
        description='Read and show line profiling results (`.lprof` files) '
        'as generated by the CLI application `kernprof` or by '
        '`LineProfiler.dump_stats()`.')
    get_main_config = functools.partial(get_cli_config, 'cli')
    default = config = get_main_config()

    add_argument(parser, '-V', '--version',
                 action='version', version=__version__)
    add_argument(parser, '-c', '--config',
                 help='Path to the TOML file, from the '
                 '`tool.line_profiler.cli` table of which to load '
                 'defaults for the options. '
                 f'(Default: {short_string_path(default.path)!r})')
    add_argument(parser, '--no-config',
                 action='store_const', dest='config', const=False,
                 help='Disable the loading of configuration files other than '
                 'the default one')
    add_argument(parser, '-u', '--unit', type=positive_float,
                 help='Output unit (in seconds) in which '
                 'the timing info is displayed. '
                 f'(Default: {default.conf_dict["unit"]} s)')
    add_argument(parser, '-r', '--rich', action='store_true',
                 help='Use rich formatting. '
                 f'(Default: {default.conf_dict["rich"]})')
    add_argument(parser, '-z', '--skip-zero', action='store_true',
                 help='Hide functions which have not been called. '
                 f'(Default: {default.conf_dict["skip_zero"]})')
    add_argument(parser, '-t', '--sort', action='store_true',
                 help='Sort by ascending total time. '
                 f'(Default: {default.conf_dict["sort"]})')
    add_argument(parser, '-m', '--summarize', action='store_true',
                 help='Print a summary of total function time. '
                 f'(Default: {default.conf_dict["summarize"]})')
    add_argument(parser, 'profile_output',
                 nargs='+',
                 help="'*.lprof' file(s) created by `kernprof`")

    args = parser.parse_args()
    if args.config:
        config = get_main_config(args.config)
        args.config = config.path
    for key, default in config.conf_dict.items():
        if getattr(args, key, None) is None:
            setattr(args, key, default)

    lstats = LineStats.from_files(*args.profile_output)
    show_text(lstats.timings, lstats.unit,
              output_unit=args.unit,
              stripzeros=args.skip_zero,
              rich=args.rich,
              sort=args.sort,
              summarize=args.summarize,
              config=args.config)


if __name__ == '__main__':
    main()
