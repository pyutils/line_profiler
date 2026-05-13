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
import sysconfig
try:
    import _pickle as pickle
except ImportError:
    import pickle  # type: ignore[assignment,no-redef]
from collections.abc import (
    Collection, Callable, Iterable, Mapping, MutableMapping,
)
from functools import partial, cached_property, wraps
from importlib import import_module
from pathlib import Path
from pickle import HIGHEST_PROTOCOL
from textwrap import indent
from threading import current_thread, main_thread
from types import FrameType, ModuleType
from typing import Any, ClassVar, Literal, TypeVar, cast, final, overload
from typing_extensions import Concatenate, ParamSpec, Self

from _line_profiler_hooks import INHERITED_PID_ENV_VARNAME, load_pth_hook
from .. import _diagnostics as diagnostics
from ..cleanup import Cleanup, _CALLBACK_REPR_HELPER
from ..curated_profiling import CuratedProfilerContext
from ..line_profiler import LineProfiler, LineStats
from ..toml_config import ConfigSource
from ._cache_logging import CacheLoggingEntry


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
_PROFILING_OUTPUT_PREFIX_PATTERN = (
    'child-prof-output-{main_pid}-{current_pid}-{prof}-'
)
_POSSIBLE_EMPTY_STATS_PREFIX_PATTERN = (
    'ignore-empty-stats-file-{main_pid}-{current_pid}-'
)


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
    # Note: if we're using the line profiler, `kernprof` always sets
    # `builtin` to true
    insert_builtin: bool = True
    debug: bool = diagnostics.DEBUG

    profiler: LineProfiler | None = _private_field(default=None)
    _sighandlers: dict[int, _SignalHandler | int | None] = (
        _private_field(default_factory=dict)
    )
    _stats_dumper: Cleanup | None = _private_field(default=None)
    # These are unstructured fields; other components can decide on what
    # to put in them. They are also pickled by `.dump()`, and are thus
    # retrievable in `.load()`-ed instances.
    _additional_data: dict[str, Any] = _private_field(default_factory=dict)

    _loaded_instance: ClassVar[LineProfilingCache | None] = None

    def __post_init__(self) -> None:
        super().__init__()

    def copy(self, /, **replacements) -> Self:
        """
        Make a copy with optionally replaced fields.

        Args:
            **replacements (Any):
                Optional fields to replace

        Return:
            inst (LineProfilingCache):
                New instance
        """
        init_args: dict[str, Any] = {}
        for field, value in self._get_init_args().items():
            init_args[field] = replacements.get(field, value)
        return type(self)(**init_args)

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
        content = {
            'init_args': self._get_init_args(),
            'additional_data': self._additional_data,
        }
        msg = f'Dumping instance data to {self.filename}: {content!r}'
        self._debug_output(msg)
        with open(self.filename, mode='wb') as fobj:
            pickle.dump(content, fobj, protocol=HIGHEST_PROTOCOL)

    def gather_stats(
        self,
        exclude_pids: Collection[int] | None = None,
        *,
        on_empty: Literal['error', 'warn', 'ignore'] = 'warn',
        on_defective: Literal['error', 'warn', 'ignore'] = 'warn',
    ) -> LineStats:
        """
        Gather the profiling output files matching ``glob_pattern`` from
        :py:attr:`~.cache_dir`, consolidating them into a single
        :py:class:`LineStats` object.

        Args:
            exclude_pids (Collection[int] | None):
                Exclude output from child processes with these PIDs;
                the default value :py:const:`None` fetches relevant
                PIDs dynamically.
            on_empty, on_defective (Literal['error', 'warn', 'ignore']):
                Passed to :py:meth:`LineStats.from_files`.

        Returns:
            :py:class:`LineStats` instance
        """
        def is_empty(path: Path) -> bool:
            return not path.stat().st_size

        filter_excludes: Callable[[Iterable[Path]], Iterable[Path]]
        if exclude_pids is None:
            # NOTE: there is no guarantee that the PID hasn't previously
            # been used for another child process that we DID properly
            # profile and SHOULD include, so we only filter out empty
            # files
            exclude_pids = self._get_pids_possibly_lacking_stats()
            filter_excludes = partial(filter, is_empty)
        else:  # User-provided values, who are we to object?
            filter_excludes = iter

        fnames_ = set(self._get_profiling_outfiles())
        for pid in exclude_pids:
            excludes = filter_excludes(self._get_profiling_outfiles(pid))
            fnames_.difference_update(excludes)
        fnames = sorted(fnames_)
        self._debug_output(
            'Loading results from {} child profiling file(s): {!r}'
            .format(len(fnames), fnames)
        )
        if not fnames:
            return LineStats.get_empty_instance()
        return LineStats.from_files(
            *fnames, on_empty=on_empty, on_defective=on_defective,
        )

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

    def _glob(self, *args, **kwargs) -> Iterable[Path]:
        return Path(self.cache_dir).glob(*args, **kwargs)

    def _get_debug_logfiles(self) -> Iterable[Path]:
        return self._glob(_DEBUG_LOG_FILENAME_PATTERN.format(
            main_pid=self.main_pid, current_pid='?*',
        ))

    def _get_profiling_outfiles(self, pid: Any = '?*') -> Iterable[Path]:
        prefix = _PROFILING_OUTPUT_PREFIX_PATTERN.format(
            main_pid=self.main_pid,
            current_pid=pid,
            # We always format the profiler ID with `hex()`, see
            # `._setup_in_child_process()`
            prof='0x?*',
        )
        return self._glob(prefix + '?*.lprof')

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

    def write_pth_hook(
        self, *,
        prefix: str | None = None,
        suffix: str | None = None,
        dir: os.PathLike[str] | str | None = None,
        # Get rid of the .pth file ASAP so as to be the least disruptive
        priority: float = 1,
        **kwargs
    ) -> Path:
        """
        Write a .pth file which allows for setting up profiling in child
        Python processes.

        Args:
            prefix, suffix (str | None):
                Optional filename-stem affixes of the .pth file; default
                is to use default values loaded from :py:attr:`.config`
            dir (os.PathLike[str] | str | None):
                Optional directory to create the .pth file in; default
                is to use ``sysconfig.get_path('purelib')``
            priority, **kwargs:
                Passed to :py:meth:`.make_tempfile`.

        Returns:
            fpath (Path):
                Path to the written .pth file
        """
        def get_pth_config() -> Mapping[str, Any]:
            # Note: the only keys in it should be `prefix` and `suffix`
            return (
                self._config_source  # Cached
                .get_subconfig('child_processes', 'pth_files')
                .conf_dict
            )

        if not os.path.exists(self.filename):
            self.dump()
            assert os.path.exists(self.filename)

        # The string casts are failsafes in case inappropriate values
        # (e.g. numbers and booleans) are supplied
        if prefix is None:
            prefix = str(get_pth_config()['prefix'])
        if suffix is None:
            suffix = str(get_pth_config()['suffix'])
        if dir is None:
            dir = sysconfig.get_path('purelib')

        template = 'import {0.__module__}; {0.__module__}.{0.__name__}({1})'
        fpath = self.make_tempfile(
            prefix=prefix, suffix=suffix + '.pth', dir=dir, priority=priority,
            **kwargs,
        )
        try:
            fpath.write_text(template.format(load_pth_hook, self.main_pid))
        except Exception:
            fpath.unlink(missing_ok=True)
            raise

        return fpath

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
              :py:meth:`.write_pth_hook`)

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
        self.write_pth_hook()
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
        def wrap_ctx_debug(
            ctx: CuratedProfilerContext, msg: str,
        ) -> None:
            self._debug_output(f'  Context {id(ctx):#x}: {msg}')

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
        if self.debug:
            self.patch(ctx, '_debug_output', wrap_ctx_debug.__get__(ctx))
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
            prefix=_PROFILING_OUTPUT_PREFIX_PATTERN.format(
                main_pid=self.main_pid,
                current_pid=os.getpid(),
                prof=hex(id(prof)),
            ),
            suffix='.lprof',
            delete=False,
        )
        dump_stats = partial(prof.dump_stats, prof_outfile)
        self.add_cleanup_with_priority(dump_stats, 1)

        # Create a cleanup object for the express purpose of dumping
        # stats in an emergency (e.g. when a signal is caught)
        self._stats_dumper = dumper = Cleanup()
        self.patch(
            dumper, '_debug_output', self._debug_output,
            cleanup=False, name=f'{self!r}._stats_dumper',
        )
        dumper.add_cleanup(dump_stats)

        # Various setups
        self._setup_common(wrap_os_fork, {'reboot_forkserver': False})

        # Set `.cleanup()` as an atexit hook to handle everything when
        # the child process is about to terminate
        atexit.register(partial(self.cleanup, reason='`atexit` callback'))

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
        # Shouldn't happen, but all kinds of weird things happen at the
        # interpreter's EoL...
        state = 'not initiated?!'
        try:
            # Just dump the stats ASAP without running `.cleanup()` to
            # avoid deadlocks
            self._debug_output(f'Caught `{name}` ({signum}), dumping stats...')
            if self._stats_dumper is not None:
                self._stats_dumper.cleanup()
        except BaseException as e:
            xc = f'{type(e).__name__}'
            msg = str(e)
            if msg:
                xc = f'{xc}: {msg}'
            state = f'failed ({xc})'
            raise e
        else:
            state = 'succeeded'
        finally:
            handler = self._sighandlers.pop(signum, None)
            msg = f'Stat-dumping {state}, passing `{name}` onto {handler!r}...'
            self._debug_output(msg)
            if handler is None:
                msg = 'original handler set from outside of Python'
                raise RuntimeError(msg)
            else:
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

    def _warn_possible_lack_of_stats(
        self, pids: int | Collection[int],
    ) -> None:
        """
        Register PID(s) which may have created a profiling stats file
        without writing to it; when calling :py:meth:`.gather_stats`,
        empty stats files associated with those PIDs are ignored instead
        of warned against or treated as an error.
        """
        if not isinstance(pids, Collection):
            pids = pids,
        with self._empty_stats_pid_registry.open(mode='a') as fobj:
            print(*pids, sep='\n', file=fobj)

    def _get_pids_possibly_lacking_stats(self) -> set[int]:
        """
        See also
            :py:meth:`._warn_possible_lack_of_stats`
        """
        prefix = _POSSIBLE_EMPTY_STATS_PREFIX_PATTERN.format(
            main_pid=self.main_pid,
            current_pid='?*',  # Gather from all child processes
        )
        result: set[int] = set()
        for registry in self._glob(prefix + '?*.dat'):
            from_reg: set[int] = set()
            with registry.open() as fobj:
                for line in fobj:
                    try:
                        from_reg.add(int(line))
                    except ValueError:
                        pass
            if from_reg:
                self._debug_output(
                    f'Loaded {len(from_reg)} PID(s) possibly lacking '
                    f'profiling output from {registry.name!r}: {from_reg!r}'
                )
                result.update(from_reg)
        return result

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
            content = pickle.load(fobj)
        instance = cls(**content['init_args'])
        instance._additional_data.update(content.get('additional_data', {}))
        return instance

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
                call = partial(wrapper, cache, vanilla_impl, *args, **kwargs)

                if debug_ is None:
                    debug_ = cache.debug
                if debug_:
                    call_fmt = cache._format_call(name, *args, **kwargs)
                    write(f'Wrapped call made: {call_fmt}...')
                    state = 'succeeded'
                    try:
                        result = call()
                    except Exception as e:
                        state = 'failed'
                        outcome = f'{type(e).__name__}'
                        if str(e):
                            outcome = f'{outcome}: {e}'
                        raise e
                    else:
                        outcome = _CALLBACK_REPR_HELPER.repr(result)
                        return result
                    finally:
                        write(f'Wrapped call {state}: {call_fmt} -> {outcome}')
                else:
                    return call()

            name = cls._get_name(vanilla_impl)
            return wrapped_impl

        for field in 'name', 'qualname', 'doc':
            dunder = f'__{field}__'
            value = getattr(wrapper, dunder, None)
            if value is not None:
                setattr(inner_wrapper, dunder, value)
        return inner_wrapper

    @classmethod
    def _format_call(
        cls, func: Callable[..., Any] | str, /, *args, **kwargs,
    ) -> str:
        if isinstance(func, partial):
            return cls._format_call(
                func.func, [*func.args, *args], {**func.keywords, **kwargs},
            )
        call = _CALLBACK_REPR_HELPER.format_call(*args, **kwargs)
        if not isinstance(func, str):
            func = cls._get_name(func)
        return func + call

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

    @cached_property
    def _config_source(self) -> ConfigSource:
        if self.config is None:
            config: str | None = None
        else:
            config = str(self.config)
        return ConfigSource.from_config(config)

    @cached_property
    def _empty_stats_pid_registry(self) -> Path:
        prefix = _POSSIBLE_EMPTY_STATS_PREFIX_PATTERN.format(
            main_pid=self.main_pid,
            current_pid=os.getpid(),
        )
        return self.make_tempfile(prefix=prefix, suffix='.dat', delete=False)
