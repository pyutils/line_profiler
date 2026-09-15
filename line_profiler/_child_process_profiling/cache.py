"""
A cache object to be used by for propagating profiling down to child
processes.
"""
from __future__ import annotations

import atexit
import dataclasses
import os
import site
import sys
import sysconfig
import warnings
try:
    import _pickle as pickle
except ImportError:
    import pickle  # type: ignore[assignment,no-redef]
from collections.abc import (
    Collection, Callable, Generator, Iterable, Mapping, MutableMapping,
)
from functools import partial, cached_property, wraps
from importlib import import_module
from pathlib import Path
from pickle import HIGHEST_PROTOCOL
from textwrap import indent
from types import ModuleType, TracebackType
from typing import Any, ClassVar, Literal, TypeVar, cast, final, overload
from typing_extensions import Concatenate, ParamSpec, Self

import _line_profiler_hooks as _lp_hooks
from .. import _diagnostics as diagnostics
from ..cleanup import Cleanup, LogLevel, _CALLBACK_REPR_HELPER
from ..curated_profiling import CuratedProfilerContext
from ..line_profiler import LineProfiler, LineStats
from ..toml_config import ConfigSource
from ._cache_logging import CacheLoggingEntry
from ._retrieve_pids import CAN_RETRIEVE_PIDS, processes_are_alive


__all__ = ('LineProfilingCache',)


T = TypeVar('T')
PS = ParamSpec('PS')

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


class _StatsHelper(Cleanup):
    def __init__(
        self,
        prof: LineProfiler,
        outfile: os.PathLike[str] | str,
        baseline: LineStats | None = None,
    ) -> None:
        super().__init__()
        self._prof = prof
        self.outfile = outfile
        self._baseline = baseline
        self.add_cleanup(self.dump)

    def __repr__(self) -> str:
        name = type(self).__name__
        func = partial(self._prof.dump_stats, self.outfile)
        repr_callback = _CALLBACK_REPR_HELPER.repr(func)
        return f'<{name} @ {hex(id(self))}: {repr_callback}>'

    def get(self) -> LineStats:
        """
        If a ``baseline`` is given (e.g. the stats state inherited
        across an :py:func:`os.fork`), subtract it first so that only
        work done in *this* process is written; the process the baseline
        was inherited from writes the baseline's contents itself.
        """
        stats = self._prof.get_stats()
        if self._baseline is not None:
            stats -= self._baseline
        return stats

    def dump(self) -> None:
        self.get().to_file(self.outfile)

    __call__ = dump

    def cleanup(self, *args, force: bool = False, **kwargs) -> None:
        if force and not any(self._current_context.values()):
            self.add_cleanup(self.dump)
        super().cleanup(*args, **kwargs)


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
    _stats_helper: _StatsHelper | None = _private_field(default=None)
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
        # `ty` needs some help here, even if we've marked the class to
        # be `@final`
        instance = cast(Self | None, cls._loaded_instance)
        if instance is None:
            pid = os.environ[_lp_hooks.INHERITED_PID_ENV_VARNAME]
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
        caveat = (
            'note that empty/malformed profiling files may be produced '
            'when child processes exits uncleanly (e.g. `.terminate()`-ed '
            'or `.kill()`-ed by the parent)'
        )
        return LineStats.from_files(
            *fnames,
            on_empty=on_empty, on_defective=on_defective,
            _note_on_empty=caveat, _note_on_defective=caveat,
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
        clean_stale: bool = True,
        **kwargs
    ) -> Path:
        """
        Write a .pth file which allows for setting up profiling in child
        Python processes.

        Args:
            prefix, suffix (str | None):
                Optional filename-stem affixes of the .pth file; default
                is to use default values loaded from :py:attr:`.config`.
            dir (os.PathLike[str] | str | None):
                Optional directory to create the .pth file in; default
                is to look up a list of directories where .pth files can
                usually be installed to, depending on the environment
                and interpreter state.
            clean_stale (bool):
                Whether to look at the directory where the .pth file
                has been written to and prune stale .pth files.
            priority, **kwargs:
                Passed to :py:meth:`.make_tempfile`.

        Returns:
            fpath (Path):
                Path to the written .pth file

        Notes:
            - Due to normalizations and attachment of extra data, users
              should NOT count on the affixes bracketing the filename
              stem of the created file.

            - During normal execution, the .pth file should be cleaned
              up, as with all other tempfiles created with
              :py:meth:`.make_tempfile` created without
              ``delete=False``. However, if the Python control flow is
              broken (e.g. process killed), cleanup can fail to occur.
              Hence the option to ``clean_stale``.
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
        prefix, suffix_template = self._normalize_pth_affixes(prefix, suffix)
        suffix = suffix_template.format(self.main_pid)

        if dir is None:
            dirs: list[Path]
            dirs = list(self._enumerate_pth_installation_locations())
        else:
            dirs = [Path(dir)]

        failures: dict[Path, str] = {}
        tempfile_msg_template = 'Created tempfile {0.name!r} at {0.parent}'
        for dir in dirs:
            try:
                fpath = self.make_tempfile(
                    prefix=prefix, suffix=suffix,
                    dir=dir, priority=priority,
                    _format_debug_msg=tempfile_msg_template.format,
                    **kwargs
                )
            except OSError as e:
                failures[dir] = self._format_exception(e)
            else:
                break
        else:
            raise RuntimeError(
                'cannot create .pth file in any of the following directories '
                f'({{path: error}}): {failures!r}'
            )

        content = self._get_graceful_oneline_call(
            _lp_hooks.__name__,
            _lp_hooks.load_pth_hook.__name__,
            self.main_pid,
        )
        try:
            fpath.write_text(content)
        except Exception:
            fpath.unlink(missing_ok=True)
            raise

        if clean_stale:
            self._cleanup_stale_pth_files(
                prefix, suffix_template, fpath.parent,
            )
        return fpath

    def _cleanup_stale_pth_files(
        self, prefix: str, suffix_template: str, dir: Path,
    ) -> None:
        def log(msg: str, level: LogLevel = 'debug') -> None:
            msg = f'._cleanup_stale_pth_files(): {msg}'
            self._debug_output(msg, level)

        def warn(msg: str) -> None:
            log(msg, 'warning')
            # 3: code calling `._cleanup_stale_pth_files()`
            warnings.warn(msg, stacklevel=3)

        if not CAN_RETRIEVE_PIDS:  # nocover
            warn(
                'lookup for stale .pth files not currently possible '
                f'on this platform (`{sys.platform}`); cleanup aborted',
            )
            return

        try:
            pth_files = self._find_pth_files(prefix, suffix_template, dir)
            is_alive = processes_are_alive(pth_files)
        except Exception as e:  # nocover
            try:
                frame = cast(TracebackType, e.__traceback__).tb_frame
                context = f'{frame.f_code.co_filename}:{frame.f_lineno}'
            except Exception:  # E.g. no traceback
                context = ''
            xc = self._format_exception(e)
            if context:
                xc = f'{xc} ({context})'
            warn(
                f'lookup for stale .pth files failed ({xc}); '
                'cleanup aborted',
            )
            return

        nfiles = sum(len(p) for p in pth_files.values())
        nstale = 0
        npruned = 0
        log(f'found {nfiles} matching .pth file(s)')
        for ppid, pths in pth_files.items():
            if is_alive[ppid]:
                log(
                    f'skipping over {len(pths)} .pth file(s) '
                    f'associated with main PID {ppid} (do not seem stale)'
                )
                continue
            for pth in pths:
                nstale += 1
                try:
                    pth.unlink()
                except Exception as e:
                    success = False
                    status = f'failed ({self._format_exception(e)})'
                else:
                    success, status = True, 'succeeded'
                    npruned += 1
                report: Callable[[str], Any] = log if success else warn
                report(f'cleanup for stale .pth file {pth.name!r} {status}')
        log(f'summary: {nfiles} found, {nstale} stale, {npruned} pruned')

    @staticmethod
    def _find_pth_files(
        prefix: str, suffix_template: str, dir: Path,
    ) -> dict[int, set[Path]]:
        result: dict[int, set[Path]] = {}
        assert suffix_template.endswith('ppid-{}.pth')
        glob_pattern = f'{prefix}?*{suffix_template.format("?*")}'
        for pth in dir.glob(glob_pattern):
            *_, suffix = pth.name.rpartition('ppid-')
            assert suffix.endswith('.pth')
            ppid = int(suffix[:-len('.pth')])
            result.setdefault(ppid, set()).add(pth)
        return result

    @staticmethod
    def _normalize_pth_affixes(prefix: str, suffix: str) -> tuple[str, str]:
        prefix = prefix.rstrip('-') + '-'
        # Escape braces because we'll use the suffix as a formatting
        # template
        suffix = suffix.strip('-').replace('{', '{{').replace('}', '}}')
        suffix_chunks: list[str] = ['ppid', '{}']
        if suffix:
            suffix_chunks.insert(0, suffix)
        suffix_template = ''.join('-' + chunk for chunk in suffix_chunks)
        suffix_template += '.pth'
        return prefix, suffix_template

    @staticmethod
    def _get_graceful_oneline_call(
        module: str, func: str, /, *args, **kwargs,
    ) -> str:
        r"""
        Get the content of a one-line .pth file which imports a
        function and calls it with the supplied arguments, and fails
        gracefully (e.g. due to failed imports) as far as possible.

        Example:
            >>> get_pth = LineProfilingCache._get_graceful_oneline_call
            >>> run = lambda stmts: exec(stmts, {})  # Isolate side-fxs

            >>> good_stmts = get_pth(
            ...     'builtins', 'print', [1, 2], 'b', None, sep='\n',
            ... )
            >>> run(good_stmts)
            [1, 2]
            b
            None

            Check that non-frozen modules are correctly handled:

            >>> pprint_stmts = get_pth(
            ...     'pprint', 'pprint', [1, 2, 3], width=5,
            ... )
            >>> run(pprint_stmts)
            [1,
             2,
             3]

            Note that it satisfies the requirements for .pth files:

            >>> assert good_stmts.startswith('import')
            >>> assert len(good_stmts.splitlines()) == 1

            If the import/module-spec lookup fails or the if the
            attribute lookup on the module fails, the code is a no-op:

            >>> from contextlib import ExitStack, redirect_stdout
            >>> from io import StringIO

            >>> with ExitStack() as stack:
            ...     fobj = stack.enter_context(StringIO())
            ...     _ = stack.enter_context(redirect_stdout(fobj))
            ...     bad_module_stmts = get_pth(
            ...         'buuiltins', 'print', 'foo',
            ...     )
            ...     run(bad_module_stmts)  # Import fails -> no-op
            ...     bad_func_stmts = get_pth(
            ...         'builtins', 'priint', 'foo',
            ...     )
            ...     run(bad_module_stmts)  # Attr lookup fails -> no-op
            ...     assert not (stdout := fobj.getvalue()), stdout

            (Note: when running with :py:mod:`xdoctest`, leaving the
            expected output empty does not by default check AGAINST
            output; hence the context contraption.)
        """
        assert module and not module.isspace()
        assert all(chunk.isidentifier() for chunk in module.split())
        assert func.isidentifier()

        call_args = [repr(a) for a in args]
        call_args.extend(f'{k}={v!r}' for k, v in kwargs.items())
        call_args = ['mod', repr(func)] + call_args

        statements = [
            'import importlib.util as iu',
            'dummy = lambda *_, **__: None',
            'call = lambda obj, attr, /, *a, **k: '
            '(func if callable(func := getattr(obj, attr, None)) else dummy)'
            '(*a, **k)',
            # Import the module
            f'spec = iu.find_spec({module!r})',
            'mod = iu.module_from_spec(spec) if spec else None',
            "call(spec.loader, 'exec_module', mod) if mod else None",
            # Retrieve and call the function
            f'call({", ".join(call_args)}) if mod else None',
        ]
        return '; '.join(statements)

    @staticmethod
    def _enumerate_pth_installation_locations() -> Generator[Path, None, None]:
        """
        Enumerate locations where the .pth file can potentially be
        installed to; the lookup order is:

        - Directory where :py:mod:`_line_profiler_hooks` is installed to
          (if among the output of :py:func:`site.getsitepackages`)

        - ``sysconfig.get_path('purelib')``

        - The output of :py:func:`site.getusersitepackages`
        """
        def filter_dirs(
            maybe_dirs: Iterable[os.PathLike[str] | str],
        ) -> Generator[Path, None, None]:
            for path in maybe_dirs:
                if os.path.isdir(path):
                    yield Path(path)

        try:
            path = Path(_lp_hooks.__file__).parent
        except Exception:
            pass
        else:
            if any(
                path.samefile(p) for p in filter_dirs(site.getsitepackages())
            ):
                yield path
        yield Path(sysconfig.get_path('purelib'))
        if site.ENABLE_USER_SITE:
            yield Path(site.getusersitepackages())

    def _debug_output(self, msg: str, /, level: LogLevel = 'debug') -> None:
        """
        Beside writing to the logger, also write to the
        :py:attr:`~._debug_log`.
        """
        entry = CacheLoggingEntry.new(self.main_pid, id(self), msg, level)
        try:
            entry.write(self._debug_log)
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
              automatically runs setup code (see
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
        try:
            self.write_pth_hook()
        except Exception as e:
            xc = self._format_exception(e)
            msg = (
                'cannot write a .pth file for setting up profiling '
                f'in child processes ({xc}); profiling data cannot be '
                'collected for non-`fork()`ed children'
            )
            self._debug_output(msg, 'warning')
            warnings.warn(msg, stacklevel=2)
        self._setup_common(wrap_os_fork, {'reboot_forkserver': True})
        self._replace_loaded_instance()

    def _setup_in_child_process(
        self,
        wrap_os_fork: bool = False,
        context: str = '',
        prof: LineProfiler | None = None,
        baseline: LineStats | None = None,
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
            baseline (LineStats | None):
                Stats which ``prof`` already held when this process
                came into existence (only meaningful for forked
                processes, which inherit the parent's profiler state);
                they are subtracted from every stats dump so that the
                pre-existing data isn't double-counted when the parent
                gathers and merges the *separately-dumped* child and
                parent stats

        Returns:
            has_set_up (bool):
                False the instance has already been set up prior to
                calling this function, true otherwise
        """
        def wrap_ctx_debug(
            ctx: CuratedProfilerContext, msg: str, /,
            level: LogLevel = 'debug',
        ) -> None:
            self._debug_output(f'  Context {id(ctx):#x}: {msg}', level)

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

        # - Occupy a tempfile slot in `.cache_dir`
        # - Set the profiler up to write thereto when the process
        #   terminates (with high priority)
        #   (Also keep a separate reference to the callback for e.g.
        #   dumping stats ASAP at process exit)
        prof_outfile = self.make_tempfile(
            prefix=_PROFILING_OUTPUT_PREFIX_PATTERN.format(
                main_pid=self.main_pid,
                current_pid=os.getpid(),
                prof=hex(id(prof)),
            ),
            suffix='.lprof',
            delete=False,
        )
        self._stats_helper = sh = _StatsHelper(prof, prof_outfile, baseline)
        self.patch(
            # If we call `sh.cleanup()` instead of `sh` (e.g. in some
            # `multiprocessing` patches), the subsequent debug-log msgs
            # are attributed to and handled by this cache instance
            sh, '_debug_output', self._debug_output,
            cleanup=False, name='<this cache object>._stats_helper',
        )
        self.add_cleanup_with_priority(sh, 1)

        # Various setups
        self._setup_common(wrap_os_fork, {'reboot_forkserver': False})

        # Set `.cleanup()` as an atexit hook to handle everything when
        # the child process is about to terminate
        atexit.register(self._atexit_hook)

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
            # Snapshot the profiler state inherited from the parent
            # BEFORE any further code runs in the fork; it is used as a
            # subtractive baseline for this process's stats dumps so
            # that pre-fork data (which the parent dumps itself) isn't
            # double-counted at gathering time
            if self.profiler is None:
                baseline = None
            else:
                baseline = self.profiler.get_stats()
            forked = self.copy()  # Ditch inherited cleanups
            forked._debug_output(f'Forked: {ppid} -> {pid}')
            if forked._replace_loaded_instance():
                forked._debug_output(
                    'Superseded cached `.load()`-ed instance in forked process'
                )
            # To avoid complications with the inherited cache instance
            # `self`:
            # - Unregister its `._atexit_hook`
            # - Discard its cleanup callbacks
            atexit.unregister(self._atexit_hook)
            self._contexts.clear()
            # Note: we can reuse the profiler instance in the fork, but
            # it needs to go through setup so that the separate
            # profiling results are dumped into another output file
            forked._setup_in_child_process(
                False, 'fork', self.profiler, baseline,
            )
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
        try:
            with self._empty_stats_pid_registry.open(mode='a') as fobj:
                print(*pids, sep='\n', file=fobj)
        except FileNotFoundError:
            # At cleanup time, the tempdir backing the cache instance
            # may already have ceased to exist; just let that be
            pass

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
            self.patch(cls, '_loaded_instance', self)
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
        *,
        debug: bool | None = None,
        wrapper_name: str | None = None,
    ) -> Callable[[Callable[PS, T]], Callable[PS, T]]:
        ...

    @overload
    @classmethod
    def _method_wrapper(
        cls, wrapper: None = None, *,
        debug: bool | None = None,
        wrapper_name: str | None = None,
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
        wrapper_name: str | None = None,
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
            wrapper_name (str | None)
                Optional name to identify the source of the wrapper,
                used in debug messages.

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
                    call_fmt = cache._format_call(
                        vanilla_name, *args, **kwargs,
                    )
                    write(
                        f'Wrapped call made via `{wrapper_name}`: '
                        f'{call_fmt}...',
                    )
                    try:
                        result = call()
                    except BaseException as e:
                        # Note: be more defensive than normal and
                        # prepared to deal with `BaseException`; this
                        # decorator is often used for functions invoked
                        # in child processes which don't cleanly
                        # terminate
                        state, outcome = 'failed', cache._format_exception(e)
                        raise e
                    else:
                        state = 'succeeded'
                        outcome = _CALLBACK_REPR_HELPER.repr(result)
                        return result
                    finally:
                        write(
                            f'Wrapped call via `{wrapper_name}` {state}: '
                            f'{call_fmt} -> {outcome}',
                        )
                else:
                    return call()

            vanilla_name = cls._get_name(vanilla_impl)
            return wrapped_impl

        if wrapper_name is None:
            wrapper_name = cls._get_name(wrapper)
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

    @staticmethod
    def _format_exception(xc: BaseException) -> str:
        formatted = type(xc).__name__
        if str(xc):
            formatted = f'{formatted}: {xc}'
        return formatted

    @property
    def environ(self) -> dict[str, str]:
        """
        Environment variables to be injected into and inherited by child
        processes.
        """
        cache_varname = f'{INHERITED_CACHE_ENV_VARNAME_PREFIX}_{self.main_pid}'
        return {
            _lp_hooks.INHERITED_PID_ENV_VARNAME: str(self.main_pid),
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
    def _consistent_with_loaded_instance(self) -> bool:
        cls = type(self)
        # Note: calling `.load()` can cause a new instance to be created
        # and stored at `._loaded_instance`, if there isn't already one
        # such instance; guard against that
        already_loaded = cls._loaded_instance
        try:
            return cls.load()._get_init_args() == self._get_init_args()
        finally:
            cls._loaded_instance = already_loaded

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

    @cached_property
    def _atexit_hook(self) -> Callable[[], None]:
        return partial(self.cleanup, reason='`atexit` callback')
