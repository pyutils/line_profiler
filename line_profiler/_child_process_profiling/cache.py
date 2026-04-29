"""
A cache object to be used by for propagating profiling down to child
processes.
"""
from __future__ import annotations

import atexit
import dataclasses
import os
import signal
import sys
try:
    import _pickle as pickle
except ImportError:
    import pickle  # type: ignore[assignment,no-redef]
from collections.abc import Collection, Callable, MutableMapping, Iterable
from functools import partial, cached_property, wraps
from importlib import import_module
from pathlib import Path
from pickle import HIGHEST_PROTOCOL
from textwrap import indent
from threading import current_thread, main_thread, RLock, Thread
from types import FrameType, ModuleType
from typing import Any, ClassVar, TypeVar, cast, final, overload
from typing_extensions import Concatenate, ParamSpec, Self

from .. import _diagnostics as diagnostics
from ..cleanup import Cleanup
from ..curated_profiling import CuratedProfilerContext
from ..line_profiler import LineProfiler, LineStats
from ._cache_logging import CacheLoggingEntry
# Note: this should have been defined here in this file, but we moved it
# over to `~._child_process_hook` because that module contains the .pth
# hook, which must run with minimal overhead when a Python process isn't
# associated with a profiled process
from .pth_hook import INHERITED_PID_ENV_VARNAME


__all__ = ('LineProfilingCache',)


T = TypeVar('T')
PS = ParamSpec('PS')
# Note: `typing.AnyStr` deprecated since 3.13
AnyStr = TypeVar('AnyStr', str, bytes)
_SignalHandler = Callable[[int, FrameType | None], Any]

_THIS_SUBPACKAGE, *_ = (lambda: None).__module__.rpartition('.')
INHERITED_CACHE_ENV_VARNAME_PREFIX = (
    'LINE_PROFILER_PROFILE_CHILD_PROCESSES_CACHE_DIR'
)
CACHE_FILENAME = 'line_profiler_cache.pkl'
_DEBUG_LOG_FILENAME_PATTERN = 'debug_log_{main_pid}_{current_pid}.log'


def _import_sibling(submodule: str) -> ModuleType:
    return import_module(f'{_THIS_SUBPACKAGE}.{submodule}')


_private_field = partial(dataclasses.field, init=False, repr=False)


@final
@dataclasses.dataclass
class LineProfilingCache(Cleanup):
    """
    Helper object for coordinating a line-profiling session, caching the
    info required to make profiling persist into child processes.
    """
    cache_dir: os.PathLike[str] | str
    config: os.PathLike[str] | str | None = None
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

    profiler: LineProfiler | None = _private_field(default=None)
    _cleanup_stacks: dict[float, list[Callable[[], Any]]] = _private_field(
        default_factory=dict,
    )
    _sighandlers: dict[int, _SignalHandler | int | None] = (
        _private_field(default_factory=dict)
    )
    _rlock: RLock = _private_field(default_factory=RLock)

    _loaded_instance: ClassVar[LineProfilingCache | None] = None

    def __post_init__(self) -> None:
        super().__init__()

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
        # `ty` needs some help here, evenif we've marked the class to be
        # `@final`
        instance = cast(Self | None, cls._loaded_instance)
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

    def cleanup(self, *args, **kwargs) -> None:
        """
        Perform cleanup.

        Args:
            *args, **kwargs
                Passed to :py:meth:`Cleanup.cleanup`

        Note:
            In child processes we set a ``SIGTERM`` handler to always
            call :py:meth:`~.cleanup`. However, this may happen when
            we're in the middle of a cleanup call, which results in
            undefined behavior. To prevent this, each
            :py:meth:`~.cleanup` call is handled by a separate thread
            which acquires an instance-specific lock.
        """
        thread = Thread(target=self._cleanup_worker, args=args, kwargs=kwargs)
        thread.start()
        thread.join()

    def _cleanup_worker(self, *args, **kwargs) -> None:
        with self._rlock:
            super().cleanup(*args, **kwargs)

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
        Gather the debug logfiles in child processes and write their
        contents to the logger
        (:py:data:`line_profiler._diagnostics.log`).

        Notes:
            - The content of each child-process log file is not
              re-parsed and is written to the logger as a single
              multi-line message.

            - To be called in the main process.
        """
        for log in sorted(self._get_debug_logfiles()):
            if log == self._debug_log:  # Don't double dip
                continue
            *_, child_pid = log.stem.rpartition('_')
            msg = 'Cache log messages from child process {}:\n{}'.format(
                child_pid, indent(log.read_text(), '  '),
            )
            diagnostics.log.debug(msg)

    def _gather_debug_log_entries(
        self, chronological: bool = False,
    ) -> list[CacheLoggingEntry]:
        """
        Gather and return all entries from debug logfiles sorted by
        timestamps.
        """
        log_files: Iterable[Path] = self._get_debug_logfiles()
        if chronological:  # Sorting on the entries -> chronological
            to_list: Callable[
                [Iterable[CacheLoggingEntry]], list[CacheLoggingEntry]
            ] = sorted
        else:
            # Otherwise, just sort by filename (entries in each file are
            # still chronological)
            log_files = sorted(log_files)
            to_list = list
        return to_list(
            entry for log in log_files
            for entry in CacheLoggingEntry.from_file(log)
        )

    def _get_debug_logfiles(self) -> Iterable[Path]:
        pattern = _DEBUG_LOG_FILENAME_PATTERN.format(
            main_pid=self.main_pid, current_pid='*',
        )
        return Path(self.cache_dir).glob(pattern)

    def inject_env_vars(
        self, env: MutableMapping[str, str] | None = None,
    ) -> None:
        """
        Inject the :py:attr:`~.environ` variables into ``env`` and add
        cleanup callbacks to reverse them.

        Args:
            env (MutableMapping[str, str] | None):
                Dictionary in the format of :py:data:`os.environ`;
                default is to use that
        """
        self.update_mapping(
            os.environ if env is None else env,
            self.environ,
            _format_debug_msg='Injecting env var ${{{1}}}: {2}'.format,
        )

    def _debug_output(self, msg: str) -> None:
        """
        Beside writing to the logger, also write to the
        :py:attr:`~._debug_log`.
        """
        try:
            self._make_debug_entry(msg).write(self._debug_log)
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

            - :py:mod:`multiprocessing` patched so that child processes
              managed thereby are properly handled

            - Instance to be returned if :py:func:`~.load()` is called
              from now on
        """
        self.dump()
        self.inject_env_vars()
        _import_sibling('pth_hook').write_pth_hook(self)
        self._setup_common(wrap_os_fork, {'reboot_forkserver': True})
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
            delete=False,
        )
        self.add_cleanup_with_priority(prof.dump_stats, 1, prof_outfile)

        # Various setups
        self._setup_common(wrap_os_fork, {'reboot_forkserver': False})

        # Set `.cleanup()` as an atexit hook to handle everything when
        # the child process is about to terminate
        atexit.register(self.cleanup)

        self._debug_output(f'Setup successful ({context})')
        return True

    def _setup_common(
        self,
        wrap_os_fork: bool,
        mp_apply_kwargs: dict[str, Any] | None = None,
    ) -> None:
        if wrap_os_fork:
            self._wrap_os_fork()
        _import_sibling('multiprocessing_patches').apply(
            self, **(mp_apply_kwargs or {}),
        )

    def _handle_signal(self, signum: int, *_) -> None:  # nocover
        """
        See also:
            :py:meth:`coverage.control.Converage._on_sigterm`
        """
        name = self._get_signal_name(signum)
        msg = f'Cleaning up before passing `{name}` ({signum}) on...'
        self._debug_output(msg)
        try:
            self.cleanup()
        finally:
            handler = self._sighandlers.pop(signum, None)
            if handler is not None:
                signal.signal(signum, handler)
            signal.raise_signal(signum)

    def _add_signal_handler(
        self, signum: int = signal.SIGTERM,
    ) -> None:  # nocover
        """
        Side effects:
            If on the main thread and not on Windows:

            - :py:func:`signal.signal` called to set
              :py:meth:`~._handle_signal` as the ``SIGTERM`` handler

            - :py:meth:`~.cleanup` callback registered undoing that

        Note:
            ``SIGTERM`` handling is known to be faulty on Windows; see
            previous discussions at (examples `1`_, `2`_).

        .. _1: https://github.com/coveragepy/coveragepy/blob/main/\
coverage/control.py
        .. _2: https://stackoverflow.com/questions/35772001/
        """
        if current_thread() != main_thread() or sys.platform == 'win32':
            return
        name = self._get_signal_name(signum)
        self._debug_output(f'Adding `{name}` handler...')
        self._sighandlers[signum] = signal.signal(signum, self._handle_signal)

    @staticmethod
    def _get_signal_name(signum: int) -> str:
        return signal.Signals(signum).name

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
            ppid = os.getpid()
            result = fork()
            if result:
                return result
            # If we're here, we are in the fork
            pid = os.getpid()
            forked = self.copy()  # Ditch inherited cleanups
            forked._debug_output(f'Forked: {ppid} -> {pid}')
            if forked._replace_loaded_instance():
                forked._debug_output(
                    'Superseded cached `.load()`-ed instance in forked process'
                )
            # Note: we can reuse the profiler instance in the fork, but
            # it needs to go through setup so that the separate
            # profiling results are dumped into another output file
            forked._setup_in_child_process(False, 'fork', self.profiler)
            return result

        self.patch(os, 'fork', wrapper, name='os')

    def make_tempfile(self, **kwargs) -> Path:
        """
        Create a fresh tempfile under :py:attr:`~.cache_dir`. The other
        arguments are passed as-is to :py:func:`tempfile.mkstemp`.

        Returns:
            path (Path):
                Path to the created file.
        """
        kwargs.setdefault('dir', self.cache_dir)
        kwargs.setdefault(
            '_format_debug_msg', 'Created tempfile: {0.name!r}'.format,
        )
        return super().make_tempfile(**kwargs)

    def _replace_loaded_instance(self, force: bool = False) -> bool:
        cls = type(self)
        if force or self._consistent_with_loaded_instance:
            cls._loaded_instance = self
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

    @overload
    @classmethod
    def _method_wrapper(
        cls,
        wrapper: Callable[Concatenate[Self, Callable[PS, T], PS], T],
        debug: bool | None = None,
    ) -> Callable[[Callable[PS, T]], Callable[PS, T]]:
        ...

    @overload
    @classmethod
    def _method_wrapper(
        cls, wrapper: None = None, debug: bool | None = None,
    ) -> Callable[
        [Callable[Concatenate[Self, Callable[PS, T], PS], T]],
        Callable[[Callable[PS, T]], Callable[PS, T]]
    ]:
        ...

    @classmethod
    def _method_wrapper(
        cls,
        wrapper: (
            Callable[Concatenate[Self, Callable[PS, T], PS], T] | None
        ) = None,
        debug: bool | None = None,
    ) -> (
        Callable[
            [Callable[Concatenate[Self, Callable[PS, T], PS], T]],
            Callable[[Callable[PS, T]], Callable[PS, T]]
        ]
        | Callable[[Callable[PS, T]], Callable[PS, T]]
    ):
        """
        Convenience wrapper decorator for functions which use the
        :py:meth:`load`-ed session instance and wrap another callable.

        Args:
            wrapper (Callable[..., T])
                Callable with the call signature
                ``(cache, vanilla_impl, *args, **kwargs) -> retval``;
                ``*args``, ``**kwargs``, and ``retval`` should be
                consistent with that of ``vanilla_impl()``'s.
            debug (bool | None)
                Whether to format and write debug messages before and
                after the call to the ``wrapper`` callable;
                if ``debug`` is not set, it will be taken from the
                session instance.

        Returns:
            inner_wrapper (Callable[[Callable[PS, T]], Callable[PS, T]])
                Wrapper(-maker) which takes the ``vanilla_impl`` and
                return a wrapper around it.
        """
        if wrapper is None:
            # `ty` doesn't quite support `partial` yet, see issue #1536
            return cast(
                Callable[[Callable[PS, T]], Callable[PS, T]],
                partial(cls._method_wrapper, debug=debug),
            )

        def inner_wrapper(vanilla_impl: Callable[PS, T]) -> Callable[PS, T]:
            @wraps(vanilla_impl)
            def wrapped_impl(*args: PS.args, **kwargs: PS.kwargs) -> T:
                cache = cls.load()
                write = cache._debug_output
                debug_: bool | None = debug
                if debug_ is None:
                    debug_ = cache.debug

                if debug_:
                    arg_reprs: list[str] = [repr(arg) for arg in args]
                    arg_reprs.extend(f'{k}={v!r}' for k, v in kwargs.items())
                    formatted_call = f'{name}({", ".join(arg_reprs)})'
                    write(f'Wrapped call made: {formatted_call}...')
                try:
                    result = wrapper(cache, vanilla_impl, *args, **kwargs)
                except Exception as e:
                    if debug_:
                        write(
                            'Wrapped call failed: '
                            f'{formatted_call} -> {type(e).__name__}: {e}',
                        )
                    raise
                else:
                    if debug_:
                        write(
                            'Wrapped call succeeded: '
                            f'{formatted_call} -> {result!r}',
                        )
                    return result

            if (
                hasattr(vanilla_impl, '__module__')
                and hasattr(vanilla_impl, '__qualname__')
            ):
                name = '{0.__module__}.{0.__qualname__}'.format(vanilla_impl)
            else:
                name = f'<anonymous function at {id(vanilla_impl):#x}>'

            return wrapped_impl

        for field in 'name', 'qualname', 'doc':
            dunder = f'__{field}__'
            value = getattr(wrapper, dunder, None)
            if value is not None:
                setattr(inner_wrapper, dunder, value)
        return inner_wrapper

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

    @cached_property
    def _make_debug_entry(self) -> Callable[[str], CacheLoggingEntry]:
        return partial(CacheLoggingEntry.new, self.main_pid, id(self))

    @cached_property
    def _consistent_with_loaded_instance(self) -> bool:
        return type(self).load()._get_init_args() == self._get_init_args()
