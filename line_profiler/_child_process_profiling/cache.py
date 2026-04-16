"""
A cache object to be used by for propagating profiling down to child
processes.
"""
from __future__ import annotations

import atexit
import dataclasses
import os
try:
    import _pickle as pickle
except ImportError:
    import pickle  # type: ignore[assignment,no-redef]
from collections.abc import Collection, Callable
from datetime import datetime
from functools import partial, cached_property, wraps
from importlib import import_module
from operator import setitem
from pathlib import Path
from pickle import HIGHEST_PROTOCOL
from reprlib import Repr
from tempfile import mkstemp
from textwrap import indent
from types import MethodType
from typing import Any, ClassVar, TypeVar, TypedDict, cast
from typing_extensions import Self, ParamSpec, Unpack

from .. import _diagnostics as diagnostics
from ..autoprofile.autoprofile import (
    # Note: we need this to equip the profiler with the
    # `.add_imported_function_or_module()` pseudo-method
    # (see `kernprof.py::_write_preimports()`), which is required for
    # the preimports to work
    _extend_line_profiler_for_profiling_imports as upgrade_profiler,
)
from ..curated_profiling import CuratedProfilerContext
from ..line_profiler import LineProfiler, LineStats
# Note: this should have been defined here in this file, but we moved it
# over to `~._child_process_hook` because that module contains the .pth
# hook, which must run with minimal overhead when a Python process isn't
# associated with a profiled process
from .pth_hook import INHERITED_PID_ENV_VARNAME


__all__ = ('LineProfilingCache',)


PS = ParamSpec('PS')
# Note: `typing.AnyStr` deprecated since 3.13
AnyStr = TypeVar('AnyStr', str, bytes)

INHERITED_CACHE_ENV_VARNAME_PREFIX = (
    'LINE_PROFILER_PROFILE_CHILD_PROCESSES_CACHE_DIR'
)
CACHE_FILENAME = 'line_profiler_cache.pkl'
_DEBUG_LOG_FILENAME_PATTERN = 'debug_log_{main_pid}_{current_pid}.log'


class _ReprAttributes(TypedDict, total=False):
    """
    Note:
        We use this typed dict instead of directly supplying them in the
        :py:meth:`_CallbackRepr.__init__()` signature, because we don't
        want to bother with the default values there.
    """
    maxlevel: int
    maxtuple: int
    maxlist: int
    maxarray: int
    maxdict: int
    maxset: int
    maxfrozenset: int
    maxdeque: int
    maxstring: int
    maxlog: int
    maxother: int
    fillvalue: str
    indent: str | int | None


class _CallbackRepr(Repr):
    """
    :py:class:`reprlib.Repr` subclass to help with representing cleanup
    callbacks, special-casing certain relevant object types (see
    examples below).

    Example:
        >>> from functools import partial
        >>>
        >>>
        >>> class MyEnviron(dict):
        ...     def some_method(self) -> None:
        ...         ...
        ...
        >>>
        >>> class MyRepr(_CallbackRepr):
        ...     # Since we can't instantiate a new `os._Environ`, test
        ...     # the relevant method with a mock
        ...     repr_MyEnviron = _CallbackRepr.repr__Environ
        ...
        >>>
        >>> r = MyRepr(maxenv=3, maxargs=4, maxstring=15)

        Environ-dict formatting:

        >>> my_env = MyEnviron(
        ...     foo='1',
        ...     bar='2',
        ...     this_varname_is_long_but_isnt_truncated=(
        ...         "THIS VALUE IS TRUNCATED BECAUSE IT'S TOO LONG"
        ...     ),
        ...     baz='4',
        ... )
        >>> print(r.repr(my_env))
        environ({'foo': '1', 'bar': '2', \
'this_varname_is_long_but_isnt_truncated': 'THIS ... LONG', ...})

        Partial-object formatting:

        >>> r.indent = 2
        >>> callback_1 = partial(int, base=8)
        >>> print(r.repr(callback_1))
        functools.partial(
          <class 'int'>,
          base=8,
        )

        >>> callback_2 = partial(min, 5, 4, 3, 2, 1)
        >>> r.indent = '----'
        >>> print(r.repr(callback_2))
        functools.partial(
        ----<built-in function min>,
        ----5,
        ----4,
        ----3,
        ----2,
        ----...,
        )

        Bound-method formatting:

        >>> r.indent = '    '
        >>> r.maxenv = 2
        >>> print(r.repr(my_env.some_method))
        <bound method MyEnviron.some_method of environ({
                                                   'foo': '1',
                                                   'bar': '2',
                                                   ...,
                                               })>
        >>> r.indent = None
        >>> r.maxenv = 0
        >>> print(r.repr(my_env.some_method))
        <bound method MyEnviron.some_method of environ({...})>
    """
    def __init__(
        self,
        *,
        maxargs: int = 5,
        maxenv: int = 3,
        **kwargs: Unpack[_ReprAttributes]
    ) -> None:
        super().__init__()  # kwargs are 3.12+
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.maxargs = maxargs
        self.maxenv = maxenv

    def repr__Environ(self, env: os._Environ[AnyStr], level: int) -> str:
        get: Callable[[AnyStr], str] = partial(self.repr1, level=level-1)
        # Truncate envvar values, but not their names
        envvars = ['{!r}: {}'.format(k, get(v)) for k, v in env.items()]
        return self._format_items(envvars, ('environ({', '})'), self.maxenv)

    def repr_method(self, method: MethodType, level: int) -> str:
        instance = self.repr1(method.__self__, level-1)
        func = getattr(method.__func__, '__qualname__', '?')
        prefix, suffix = f'<bound method {func} of ', '>'
        # Take care of possible multi-line reprs
        return _indent_with_prefix(instance, prefix) + suffix

    def repr_partial(self, ptl: partial, level: int) -> str:
        get: Callable[[Any], str] = partial(self.repr1, level=level-1)
        args = [get(arg) for arg in ptl.args]
        args.extend('{}={}'.format(k, get(v)) for k, v in ptl.keywords.items())
        args.insert(0, get(ptl.func))
        name = '{0.__module__}.{0.__qualname__}'.format(type(ptl))
        # The +1 is to account for `ptl.func`
        return self._format_items(args, (name + '(', ')'), self.maxargs + 1)

    def _format_items(
        self,
        items: Collection[str],
        delims: tuple[str, str],
        maxlen: int | None = None,
    ) -> str:
        start, end = delims
        if maxlen is not None and len(items) > maxlen:
            items = list(items)[:maxlen] + ['...']
        if self.indent is None or not items:
            return '{}{}{}'.format(start, ', '.join(items), end)
        return '\n'.join([
            start, *(indent(item + ',', self.indent) for item in items), end,
        ])

    @property
    def indent(self) -> str | None:
        return self._indent

    @indent.setter
    def indent(self, indent: str | int | None) -> None:
        if indent is None or isinstance(indent, str):
            self._indent = indent
            return
        self._indent = ' ' * indent


_CALLBACK_REPR = _CallbackRepr(maxother=cast(int, float('inf'))).repr


@dataclasses.dataclass
class LineProfilingCache:
    """
    Helper object for coordinating a line-profiling session, caching the
    info required to make profiling persist into child processes.
    """
    cache_dir: os.PathLike[str] | str
    profiling_targets: Collection[str] = dataclasses.field(
        default_factory=list,
    )
    rewrite_module: os.PathLike[str] | str | None = None
    profile_imports: bool = False
    preimports_module: os.PathLike[str] | str | None = None
    main_pid: int = dataclasses.field(default_factory=os.getpid)
    # Note: if we're using the line profiler, `kernprof` always set
    # `builtin` to true
    insert_builtin: bool = True
    debug: bool = diagnostics.DEBUG

    profiler: LineProfiler | None = dataclasses.field(default=None, init=False)
    _cleanup_stacks: dict[float, list[Callable[[], Any]]] = dataclasses.field(
        default_factory=dict, init=False,
    )
    _loaded_instance: ClassVar[Self | None] = None

    def cleanup(self) -> None:
        """
        Pop all the cleanup callbacks from the internal stack added via
        :py:meth:`~.add_cleanup` and call them in order.
        """
        for priority in sorted(self._cleanup_stacks):
            callbacks = self._cleanup_stacks.pop(priority)
            while callbacks:
                callback = callbacks.pop()
                callback_repr = _CALLBACK_REPR(callback)
                try:
                    callback()
                except Exception as e:
                    msg = f'failed: {callback_repr}: {type(e).__name__}: {e}'
                else:
                    msg = f'succeeded: {callback_repr}'
                self._debug_output('Cleanup ' + msg)

    def add_cleanup(
        self, callback: Callable[PS, Any], *args: PS.args, **kwargs: PS.kwargs,
    ) -> None:
        """
        Add a cleanup callback to the internal stack; they can be later
        called by :py:meth:`~.cleanup`.
        """
        self._add_cleanup(callback, 0, *args, **kwargs)

    def _add_cleanup(
        self, callback: Callable[PS, Any], priority: float,
        *args: PS.args, **kwargs: PS.kwargs,
    ) -> None:
        if args or kwargs:
            callback = partial(callback, *args, **kwargs)
        self._cleanup_stacks.setdefault(priority, []).append(callback)
        header = 'Cleanup callback added'
        if priority:
            header = f'{header} (priority: {priority})'
        self._debug_output(f'{header}: {_CALLBACK_REPR(callback)}')

    def copy(
        self, *,
        inherit_cleanups: bool = False,
        inherit_profiler: bool = False,
        **replacements
    ) -> Self:
        """
        Make a copy with optionally replaced fields.

        Args:
            inherit_cleanups (bool):
                If true, the copy also makes a (shallow) copy of the
                cleanup-callback stack.
            inherit_profiler (bool):
                If true, the copy also gets a reference to
                :py:attr:`~.profiler`
            **replacements (Any):
                Optional fields to replace

        Return:
            inst (LineProfilingCache):
                New instance
        """
        init_args: dict[str, Any] = {}
        for field, value in self._get_init_args().items():
            init_args[field] = replacements.get(field, value)
        copy = type(self)(**init_args)
        if inherit_cleanups:
            copy._cleanup_stacks = {
                priority: list(callbacks)
                for priority, callbacks in self._cleanup_stacks.items()
            }
        if inherit_profiler:
            copy.profiler = self.profiler
        return copy

    @classmethod
    def load(cls) -> Self:
        """
        Reconstruct the instance from the environment variables
        :env:`LINE_PROFILER_PROFILE_CHILD_PROCESSES_CACHE_PID` and
        :env:`LINE_PROFILER_PROFILE_CHILD_PROCESSES_CACHE_DIR_<PID>`.
        These should have been set from an ancestral Python process.

        Note:
            If a previously :py:meth:`.~.load`-ed instance exists, it is
            returned instead of a new instance.
        """
        instance = cls._loaded_instance
        if instance is None:
            pid = os.environ[INHERITED_PID_ENV_VARNAME]
            cache_varname = f'{INHERITED_CACHE_ENV_VARNAME_PREFIX}_{pid}'
            cache_dir = os.environ[cache_varname]
            msg = (
                f'PID {os.getpid()} (from {pid}): '
                f'Loading instance from ${{{cache_varname}}} = {cache_dir}'
            )
            diagnostics.log.debug(msg)
            instance = cls._from_path(cls._get_filename(cache_dir))
            instance._replace_loaded_instance(force=True)
        return instance

    def dump(self) -> None:
        """
        Serialize the cache instance and dump into the default location
        as indicated by :py:attr:`~.cache_dir`, so that they can be
        :py:meth:`~.load`-ed by child processes.

        Note:
            Cleanup callbacks are not serialized.
        """
        content = self._get_init_args()
        msg = f'Dumping instance data to {self.filename}: {content!r}'
        self._debug_output(msg)
        with open(self.filename, mode='wb') as fobj:
            pickle.dump(content, fobj, protocol=HIGHEST_PROTOCOL)

    def gather_stats(self, glob_pattern: str = '*.lprof') -> LineStats:
        """
        Gather the profiling output files matching ``glob_pattern`` from
        :py:attr:`~.cache_dir`, consolidating them into a single
        :py:class:`LineStats` object.
        """
        fnames = list(Path(self.cache_dir).glob(glob_pattern))
        self._debug_output(
            'Loading results from {} child profiling file(s): {!r}'
            .format(len(fnames), fnames)
        )
        if not fnames:
            return LineStats.get_empty_instance()
        return LineStats.from_files(*fnames, on_defective='ignore')

    def _dump_debug_logs(self) -> None:
        """
        Gather the debug logfiles in child processes and write them to
        the logger; to be called in the main process.
        """
        pattern = _DEBUG_LOG_FILENAME_PATTERN.format(
            main_pid=self.main_pid, current_pid='*',
        )
        for log in sorted(Path(self.cache_dir).glob(pattern)):
            if log == self._debug_log:  # Don't double dip
                continue
            *_, child_pid = log.stem.rpartition('_')
            msg = 'Cache log messages from child process {}:\n{}'.format(
                child_pid, indent(log.read_text(), '  '),
            )
            diagnostics.log.debug(msg)

    def inject_env_vars(
        self, env: dict[str, str] | None = None,
    ) -> None:
        """
        Inject the :py:attr:`~.environ` variables into ``env`` and add
        cleanup callbacks to reverse them.

        Args:
            env (dict[str, str] | None):
                Dictionary in the format of :py:data:`os.environ`;
                default is to use that
        """
        if env is None:
            env = cast(dict[str, str], os.environ)
        for name, value in self.environ.items():
            try:
                old = env[name]
            except KeyError:
                self.add_cleanup(env.pop, name, None)
                change = f'{value!r} (new)'
            else:
                self.add_cleanup(setitem, env, name, old)
                change = f'{old!r} -> {value!r}'
            self._debug_output(f'Injecting env var ${{{name}}}: {change}')
            env[name] = value

    def _debug_output(self, msg: str) -> None:
        msg = f'{self._debug_message_header}: {msg}'
        diagnostics.log.debug(msg)
        if not self._debug_log:
            return
        try:
            with self._debug_log.open(mode='a') as fobj:
                prefix = self._debug_message_timestamp + ' '
                print(_indent_with_prefix(msg, prefix), file=fobj)
        except OSError:  # Cache dir may have been rm-ed during cleanup
            pass

    def _setup_in_main_process(self, wrap_os_fork: bool = True) -> None:
        """
        Set up shop in the main process so that (line-)profiling can
        extend into child processes.

        Args:
            wrap_os_fork (bool):
                Whether to wrap :py:func:`os.fork` which handles
                profiling

        Side effects:

            - Instance data written to :py:attr:`~.cache_dir`

            - Environment variables injected
              (see :py:meth:`~.inject_env_vars()`)

            - A ``.pth`` file written so that child processes
              automaticaly runs setup code (see
              :py:func:`line_profiler._child_process_hook.pth_hook.\
write_pth_hook`)

            - :py:func:`os.fork` wrapped so that profiling set up in
              forked processes is properly handled (if
              ``wrap_os_fork=True``)

            - :py:mod:`multiprocessing` wrapped so that child processes
              managed by the package are properly handled

            - Instance to be returned if :py:func:`~.load()` is called
              from now on
        """
        this_subpkg, *_, _ = (lambda: None).__module__.rpartition('.')

        self.dump()
        self.inject_env_vars()
        pth_hook = import_module(this_subpkg + '.pth_hook')
        pth_hook.write_pth_hook(self)

        self._wrap_os_fork()
        mp_patches = import_module(this_subpkg + '.multiprocessing_patches')
        mp_patches.apply(self)

        self._replace_loaded_instance()

    def _setup_in_child_process(
        self,
        wrap_os_fork: bool = False,
        context: str = '',
        prof: LineProfiler | None = None,
    ) -> bool:
        """
        Set up shop in a forked/spawned child process so that
        (line-)profiling can extend therein.

        Args:
            wrap_os_fork (bool):
                Whether to wrap :py:func:`os.fork` which handles
                profiling; already-forked child processes should set
                this to false
            context (str):
                Optional context from which the function is called, to
                be used in log messages
            prof (LineProfiler | None):
                Optional profiler instance to associate with the cache;
                if not provided, an instance is created

        Returns:
            has_set_up (bool):
                False the instance has already been set up prior to
                calling this function, true otherwise
        """
        if not context:
            context = '...'
        self._debug_output(f'Setting up ({context})...')
        if self.profiler is not None:  # Already set up
            self._debug_output(f'Setup aborted ({context})')
            return False

        # Create a profiler instance and manage it with
        # `CuratedProfilerContext`
        if prof is None:
            prof = LineProfiler()
        self.profiler = prof
        upgrade_profiler(prof)
        ctx = CuratedProfilerContext(prof, insert_builtin=self.insert_builtin)
        ctx.install()
        self.add_cleanup(ctx.uninstall)
        self._debug_output(f'Set up `.profiler` at {id(prof):#x}')

        # Do the preimports at `cache.preimports_module` where
        # appropriate
        if self.preimports_module:
            self._debug_output('Loading preimports...')
            with open(self.preimports_module, mode='rb') as fobj:
                code = compile(fobj.read(), self.preimports_module, 'exec')
                exec(code, {})  # Use a fresh, empty namespace

        # Occupy a tempfile slot in `.cache_dir` and set the profiler
        # up to write thereto when the process terminates (with high
        # priority)
        prof_outfile = self.make_tempfile(
            prefix='child-prof-output-{}-{}-{:#x}-'
            .format(self.main_pid, os.getpid(), id(prof)),
            suffix='.lprof',
        )
        self._add_cleanup(prof.dump_stats, -1, prof_outfile)

        # Set up `os.fork()` wrapping if needed (i.e. in a spawned
        # process)
        if wrap_os_fork:
            self._wrap_os_fork()

        # Set `.cleanup()` as an atexit hook to handle everything when
        # the child process is about to terminate
        atexit.register(self.cleanup)

        self._debug_output(f'Setup successful ({context})')
        return True

    def _wrap_os_fork(self) -> None:
        """
        Create a wrapper around :py:func:`os.fork` which handles
        profiling.

        Side effects:
            - :py:func:`os.fork` (if available) replaced with the
              wrapper
            - :py:meth:`~.cleanup` callback registered undoing that
        """
        try:
            fork = os.fork
        except AttributeError:  # Can't fork on this platform
            return

        @wraps(fork)
        def wrapper() -> int:
            result = fork()
            if result:
                return result
            # If we're here, we are in the fork
            forked = self.copy()  # Ditch inherited cleanups
            if forked._replace_loaded_instance():
                forked._debug_output(
                    'Superseded cached `.load()`-ed instance in forked process'
                )
            # Note: we can reuse the profiler instance in the fork, but
            # it needs to go through setup so that the separate
            # profiling results are dumped into another output file
            forked._setup_in_child_process(False, 'fork', self.profiler)
            return result

        # Note: type checkers have vastly different opinions on
        # `os.fork = wrapper`:
        # - `ty` wouldn't shut up about shadowing unless we explicitly
        #   type-annotate the assignment (error type:
        #   "invalid-assignment")
        # - `mypy` is cool with the bare assignment, but complains about
        #   declaring types in "assignment to non-self-attribute"
        #   (error type: "misc")
        # The only way to satisfy both seems to be either an
        # unqualified "type: ignore" comment, or circumventing the
        # attribute checks by `setattr()` or assigning to the module
        # namespace dict.
        os.fork = wrapper  # type: ignore
        self.add_cleanup(setattr, os, 'fork', fork)

    def make_tempfile(self, **kwargs) -> Path:
        """
        Create a fresh tempfile under :py:attr:`~.cache_dir`. The other
        arguments are passed as-is to :py:func:`tempfile.mkstemp`.

        Returns:
            path (Path):
                Path to the created file.
        """
        handle, path = mkstemp(dir=self.cache_dir, **kwargs)
        try:
            path_obj = Path(path)
            self._debug_output(f'Created tempfile: {path_obj.name!r}')
            return path_obj
        finally:
            os.close(handle)

    def _replace_loaded_instance(self, force: bool = False) -> bool:
        cls = type(self)
        if force or self._consistent_with_loaded_instance:
            # Note: `ty` REALLY hates assigning an instance to
            # `ClassVar[Self]` (#3274); no choice but to ignore it for
            # the time being...
            cls._loaded_instance = self  # type: ignore
            return True
        return False

    @classmethod
    def _from_path(cls, fname: os.PathLike[str] | str) -> Self:
        with open(fname, mode='rb') as fobj:
            return cls(**pickle.load(fobj))

    def _get_init_args(self) -> dict[str, Any]:
        init_fields = [
            field_obj.name for field_obj in dataclasses.fields(self)
            if field_obj.init
        ]
        return {name: getattr(self, name) for name in init_fields}

    @staticmethod
    def _get_filename(cache_dir: os.PathLike[str] | str) -> str:
        return os.path.join(cache_dir, CACHE_FILENAME)

    @property
    def environ(self) -> dict[str, str]:
        """
        Environment variables to be injected into and inherited by child
        processes.
        """
        cache_varname = f'{INHERITED_CACHE_ENV_VARNAME_PREFIX}_{self.main_pid}'
        return {
            INHERITED_PID_ENV_VARNAME: str(self.main_pid),
            cache_varname: str(self.cache_dir),
        }

    @property
    def filename(self) -> str:
        return self._get_filename(self.cache_dir)

    @property
    def _debug_log(self) -> Path | None:
        if not self.debug:
            return None
        fname = _DEBUG_LOG_FILENAME_PATTERN.format(
            main_pid=self.main_pid, current_pid=os.getpid(),
        )
        return Path(self.cache_dir) / fname

    @property
    def _debug_message_timestamp(self) -> str:
        return f'[cache-debug-log {datetime.now()} DEBUG]'

    @cached_property
    def _debug_message_header(self) -> str:
        pid = os.getpid()
        return 'PID {} ({}): Cache {:#x}'.format(
            pid,
            'main process' if self.main_pid == pid else self.main_pid,
            id(self),
        )

    @cached_property
    def _consistent_with_loaded_instance(self) -> bool:
        return type(self).load()._get_init_args() == self._get_init_args()


def _indent_with_prefix(string: str, prefix: str, fill_char: str = ' ') -> str:
    width = len(prefix)
    return prefix + indent(string, fill_char * width)[width:]
