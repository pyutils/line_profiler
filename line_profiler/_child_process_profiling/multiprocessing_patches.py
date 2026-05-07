"""
Patch :py:mod:`multiprocessing` so that profiling extends into processes
it creates.

Notes:
    - Based on the implementations in :py:mod:`coverage.multiproc` and
      :py:mod:`pytest_autoprofile._multiprocessing`.

    - Results may vary if the process pool is not properly
      :py:meth:`multiprocessing.pool.Pool.close`-d and
      :py:meth:`multiprocessing.pool.Pool.join`-ed;
      see `this caveat <https://coverage.readthedocs.io/\
en/latest/subprocess.html#using-multiprocessing>`__.
"""
from __future__ import annotations

import dataclasses
import multiprocessing
import warnings
from collections.abc import Callable, Collection, Mapping, Sequence
from functools import partial
from importlib import import_module
from inspect import getattr_static, signature
from multiprocessing.process import BaseProcess
from multiprocessing.pool import Pool
from operator import attrgetter
from time import sleep, monotonic
from types import MappingProxyType, ModuleType
from typing import (
    TYPE_CHECKING,
    Any, ClassVar, Generic, Literal, NamedTuple, Protocol, TypeVar, NoReturn,
    cast, final,
)

from typing_extensions import Concatenate, ParamSpec, Self

try:
    from multiprocessing import spawn
except ImportError:
    _CAN_USE_SPAWN = False
else:
    _CAN_USE_SPAWN = True
try:
    from multiprocessing import forkserver
except ImportError:
    _CAN_USE_FORKSERVER = False
else:
    _CAN_USE_FORKSERVER = (
        'forkserver' in multiprocessing.get_all_start_methods()
    )

from .. import _diagnostics as diagnostics
from ..toml_config import ConfigSource
from .cache import LineProfilingCache
from .runpy_patches import create_runpy_wrapper


__all__ = ('apply',)


T = TypeVar('T')
T1 = TypeVar('T1')
T2 = TypeVar('T2')
PS = ParamSpec('PS')
PS1 = ParamSpec('PS1')
PS2 = ParamSpec('PS2')
_OnTimeout = Literal['ignore', 'warn', 'error']
_PatchName = Literal['pool', 'process', 'logging']

_PATCHED_MARKER = '__line_profiler_patched_multiprocessing__'
_LOGGERS = ['sub_debug', 'debug', 'info', 'sub_warning', 'warn']

# ------------------------------ Helpers -------------------------------


class _Wrapper(Protocol, Generic[PS, T]):
    def __call__(self, func: Callable[PS, T], /) -> Callable[PS, T]:
        ...


class _Poller:
    """
    Poll a callable until it returns true-y.

    Example:
        >>> import warnings
        >>> from contextlib import ExitStack
        >>> from functools import partial
        >>> from itertools import count
        >>> from typing import Iterator, Literal

        >>> def count_until(
        ...     limit: int, mode: Literal['until', 'while'] = 'until',
        ... ) -> bool:
        ...     def counter_is_big_enough(
        ...         counter: Iterator[int], limit: int,
        ...     ) -> bool:
        ...         return next(counter) >= limit
        ...
        ...     def counter_is_small_enough(
        ...         counter: Iterator[int], limit: int,
        ...     ) -> bool:
        ...         return next(counter) < limit
        ...
        ...     # The branches are ultimately equal in results, but we
        ...     # want to explicitly test both `.poll_until()` and
        ...     # `.poll_while()`
        ...     if mode == 'until':
        ...         get_poller = partial(
        ...             _Poller.poll_until, counter_is_big_enough,
        ...         )
        ...     else:
        ...         get_poller = partial(
        ...             _Poller.poll_while, counter_is_small_enough,
        ...         )
        ...     return get_poller(count(), limit)

        >>> with count_until(10).with_cooldown(.01).with_timeout(1):
        ...     # Note: we shouldn't really need that much time, but
        ...     # something in CI seems to be slowing down the polling
        ...     # loop...
        ...     print('We counted up to 10')
        We counted up to 10

        >>> with (
        ...     count_until(100)
        ...     .with_cooldown(.01)
        ...     .with_timeout(.5)  # `[on_]timeout` separately supplied
        ...     .with_timeout(on_timeout='ignore')
        ... ):
        ...     print("We probably didn't count up to 100 but whatever")
        We probably didn't count up to 100 but whatever

        >>> with (  # doctest: +NORMALIZE_WHITESPACE
        ...     count_until(30).with_cooldown(.01).with_timeout(.25)
        ... ):
        ...     print('We counted up to 30')
        Traceback (most recent call last):
          ...
        line_profiler..._Poller.Timeout: ...
        timed out (... s >= 0.25 s) waiting for
        callback ...counter_is_big_enough... to return true

        >>> with ExitStack() as stack:  # doctest: +NORMALIZE_WHITESPACE
        ...     enter = stack.enter_context
        ...     enter(warnings.catch_warnings())
        ...     warnings.simplefilter('error', _Poller.TimeoutWarning)
        ...     enter(
        ...         count_until(30, 'while')
        ...         .with_cooldown(.01)
        ...         .with_timeout(.25, 'warn')
        ...     )
        ...     print('We counted up to 30 again')
        Traceback (most recent call last):
          ...
        line_profiler..._Poller.TimeoutWarning: ...
        timed out (... s >= 0.25 s) waiting for
        callback ...counter_is_small_enough... to return true
    """
    def __init__(
        self,
        func: Callable[[], Any],
        cooldown: float = 0,
        timeout: float = 0,
        on_timeout: _OnTimeout = 'error',
    ) -> None:
        self._func: Callable[[], Any] = func
        self._cooldown = max(0, cooldown)
        self._timeout = max(0, timeout)
        self._on_timeout = on_timeout

    def sleep(self):
        cd = self._cooldown
        if cd > 0:
            sleep(cd)

    def with_cooldown(self, cooldown: float) -> Self:
        return type(self)(
            self._func, cooldown, self._timeout, self._on_timeout,
        )

    def with_timeout(
        self,
        timeout: float | None = None,
        on_timeout: _OnTimeout | None = None,
    ) -> Self:
        if timeout is None:
            timeout = self._timeout
        if on_timeout is None:
            on_timeout = self._on_timeout
        return type(self)(self._func, self._cooldown, timeout, on_timeout)

    @classmethod
    def poll_until(
        cls, func: Callable[PS, Any], /, *args: PS.args, **kwargs: PS.kwargs
    ) -> Self:
        if args or kwargs:
            func = partial(func, *args, **kwargs)
        return cls(func)

    @classmethod
    def poll_while(
        cls, func: Callable[PS, Any], /, *args: PS.args, **kwargs: PS.kwargs
    ) -> Self:
        def negated(
            func: Callable[PS, Any], *a: PS.args, **k: PS.kwargs
        ) -> bool:
            return not func(*a, **k)

        return cls(partial(negated, func, *args, **kwargs))

    def __enter__(self) -> Self:
        def error(msg: str) -> NoReturn:
            raise type(self).Timeout(msg)

        def warn(msg: str) -> None:
            # Write log before issuing the warning because that may be
            # promoted to an exception
            diagnostics.log.warning(msg)
            warnings.warn(msg, type(self).TimeoutWarning, stacklevel=3)

        timeout = self._timeout
        callback = self._func

        handle_timeout: Callable[[str], Any] = {
            'error': error, 'warn': warn, 'ignore': _no_op,
        }[self._on_timeout]
        fmt = '.3g'
        timeout_msg_header = f'{type(self).__name__} at {id(self):#x}'

        start = monotonic()
        while not callback():
            elapsed = monotonic() - start
            if timeout and elapsed >= timeout:
                handle_timeout(
                    f'{timeout_msg_header}: '
                    f'timed out ({elapsed:{fmt}} s >= {timeout:{fmt}} s) '
                    f'waiting for callback {callback!r} to return true'
                )
                break
            self.sleep()
        return self

    def __exit__(self, *_, **__) -> None:
        pass

    class Timeout(RuntimeError):
        """
        Raised when a :py:class:`_Poller` is timed out when polling.
        """
        pass

    class TimeoutWarning(Timeout, UserWarning):
        """
        Issued when a :py:class:`_Poller` is timed out when polling.
        """
        pass


@final
class _PollerArgs(NamedTuple):
    cooldown: float
    timeout: float
    on_timeout: str | None

    @classmethod
    def from_config(cls, config: ConfigSource) -> Self:
        values = _get_config(config)['polling']
        try:
            cooldown = max(float(values['cooldown']), 0)
        except (TypeError, ValueError):
            cooldown = 0
        try:
            timeout = max(float(values['timeout']), 0)
        except (TypeError, ValueError):
            timeout = 0
        try:
            on_timeout: str | None = values['on_timeout'].lower()
        except Exception:  # Fallback (use `_Poller`'s default)
            on_timeout = None
        return cls(cooldown, timeout, on_timeout)

    @classmethod
    def get_defaults(cls) -> Self:
        namespace = globals()
        try:
            return namespace['_DEFAULT_POLLER_ARGS']
        except KeyError:
            defaults = cls.from_config(ConfigSource.from_default(copy=False))
            return namespace.setdefault('_DEFAULT_POLLER_ARGS', defaults)


class TaskWrapper(Generic[PS, T]):
    """
    Pickle-able wrapper around the supplied task callable, which writes
    to the session's profiling-stats file on exit.
    """
    def __init__(self, func: Callable[PS, T]) -> None:
        self.func = func
        try:
            self.__signature__ = signature(func)
        except Exception:  # nocover
            # Can happen with e.g. certain builin/c-based callables
            pass

    def __call__(self, *args, **kwargs) -> T:
        dump_stats = LineProfilingCache.load()._dump_stats
        try:
            return self.func(*args, **kwargs)
        finally:
            if dump_stats is not None:
                dump_stats()


@dataclasses.dataclass
class Patch:
    """
    Patch to apply to a component in :py:mod:`multiprocessing`.

    Attributes:
        submodule (str):
            Name of the :py:mod:`multiprocessing` submodule.
        targets (dict[str,\
dict[str, Callable[[Any], Any] | Sequence[Callable[[Any], Any]]]]):
            Dictionary mapping (dot-chained) names in said submodule to
            a dictionary of patches; said patches dictionary should have
            the format of
            ``dict[simple_attribute, wrapper | [wrapper1, ...]]``. See
            Example for details.

    Example:
        Consider
        ``Patch('foo', {'bar.baz': {'foobar': foofoo},\
'': {'spam': [ham, eggs]}})``.
        This instance would perform the following patches on the module
        ``multiprocessing.foo``:

        - Replace ``multiprocessing.foo.bar.baz.foobar`` with
          ``foofoo(multiprocessing.foo.bar.baz.foobar)``

        - Replace ``multiprocessing.foo.spam`` with
          ``eggs(ham(multiprocessing.foo.spam))``;
          note that the two wrappers are applied in order to the
          original attribute.
    """
    submodule: str
    targets: dict[
        str, dict[str, Callable[[Any], Any] | Sequence[Callable[[Any], Any]]]
    ] = dataclasses.field(default_factory=dict)
    package: ClassVar[str] = 'multiprocessing'

    def add_target(
        self,
        target: str,
        patches: Mapping[
            str, Callable[[Any], Any] | Sequence[Callable[[Any], Any]]
        ],
    ) -> Self:
        """
        Convenience method for gradually constructing the patch with a
        fluent interface.

        Returns:
            This instance
        """
        self.targets.setdefault(target, {}).update(patches)
        return self

    def apply(
        self,
        cache: LineProfilingCache,
        *,
        cleanup: bool = True,
        static: bool = True,
    ) -> list[str]:
        """
        Apply the patch.

        Args:
            cache (LineProfilingCache):
                Session cache
            cleanup (bool):
                Whether ``cache.cleanup()`` should reverse the patches
            static (bool):
                Whether to use :py:func:`inspect.getattr_static` to
                retrieve to the attributes to be patched on the patch
                targets

        Returns:
            replacements (list[str]):
                Names of entities replaced
        """
        submod_name = f'{self.package}.{self.submodule}'
        get_attribute = getattr_static if static else getattr
        result: list[str] = []
        try:
            mod = self.load_module()
        except ImportError:  # nocover
            return []

        for target in sorted(self.targets, key=len, reverse=True):
            if TYPE_CHECKING:
                # See `ty` issue #2572
                assert isinstance(target, str)
            if target:
                try:
                    obj: Any = attrgetter(target)(mod)
                except AttributeError:  # nocover
                    continue
                name = f'{submod_name}.{target}'
            else:
                obj, name = mod, submod_name
            replace = partial(cache.patch, obj, cleanup=cleanup, name=name)
            for method, method_wrappers in self.targets[target].items():
                if callable(method_wrappers):
                    method_wrappers = cast(
                        Sequence[Callable[[Any], Any]], (method_wrappers,),
                    )
                try:
                    impl = get_attribute(obj, method)
                except AttributeError:
                    continue
                for wrapper in method_wrappers:
                    impl = wrapper(impl)
                replace(method, impl)
                result.append(f'{name}.{method}')
        return result

    def load_module(self) -> ModuleType:
        """
        Returns:
            Module object :py:attr:`.module` points to
        """
        return import_module(self.module)

    @staticmethod
    def _join(s: str, *strs: str, sep: str = '.') -> str:
        return sep.join(string for string in (s, *strs) if string)

    @property
    def module(self) -> str:
        """
        Module where the patches are applied
        """
        return self._join(self.package, self.submodule)

    @property
    def summary(self) -> MappingProxyType[str, frozenset[str]]:
        """
        Summary of the dotted paths to the patched objects and their
        patched attributes
        """
        add_prefix = partial(self._join, self.module)
        return MappingProxyType({
            add_prefix(target): frozenset(patches)
            for target, patches in self.targets.items()
        })


def _get_config(config: ConfigSource) -> Mapping[str, Any]:
    cd = dict(
        config.get_subconfig('child_processes', 'multiprocessing', copy=True)
        .conf_dict
    )
    assert isinstance(cd.get('patches'), Mapping)
    assert isinstance(cd.get('polling'), Mapping)
    return MappingProxyType({
        **cd,
        'patches': MappingProxyType(cd['patches']),
        'polling': MappingProxyType(cd['polling']),
    })


def _process_has_returned(
    proc: BaseProcess, cache: LineProfilingCache, timeout: float,
) -> bool:
    popen = getattr(proc, '_popen', None)
    if popen is None:
        msg, result = 'No associated process', True
    else:
        result = popen.wait(timeout) is not None
        if result:
            msg = f'Process {popen.pid} has returned'
        else:
            msg = f'Waiting for process {popen.pid} to return...'
    cache._debug_output(f'  {type(proc).__name__} @ {id(proc):#x}: {msg}')
    return result


def _no_op(*_, **__) -> None:
    pass


# ---------------- `multiprocessing.pool.Pool` patches -----------------


@LineProfilingCache._method_wrapper
def wrap_get_tasks(
    _,  # No need to use the cache, but `_method_wrapper` expects it
    vanilla_impl: Callable[Concatenate[Callable[PS1, T1], PS2], T2],
    func: Callable[PS1, T1],
    *args: PS2.args,
    **kwargs: PS2.kwargs
) -> T2:
    """
    Wrap around :py:meth:`.Pool._get_tasks` so that the writing of
    profiling stats is handled within the callables sent to the child
    processes before the parent process assumes control.

    Note:
        :py:meth:`Pool._get_tasks` is a static method.
    """
    return vanilla_impl(TaskWrapper(func), *args, **kwargs)


@LineProfilingCache._method_wrapper
def wrap_guarded_task_generation(
    _,  # No need to use the cache, but `_method_wrapper` expects it
    vanilla_impl: Callable[Concatenate[Pool, int, Callable[PS1, T1], PS2], T2],
    self: Pool,
    result_job: int,
    func: Callable[PS1, T1],
    *args: PS2.args,
    **kwargs: PS2.kwargs
) -> T2:
    """
    Wrap around :py:meth:`.Pool._guarded_task_generation` so that the
    writing of profiling stats is handled within the callables sent to
    the child processes before the parent process assumes control.
    """
    return vanilla_impl(self, result_job, TaskWrapper(func), *args, **kwargs)


# ----------- `multiprocessing.process.BaseProcess` patches ------------


@LineProfilingCache._method_wrapper
def wrap_terminate(
    cache: LineProfilingCache,
    vanilla_impl: Callable[[BaseProcess], None],
    self: BaseProcess,
) -> None:
    """
    Wrap around :py:meth:`BaseProcess.terminate` to make sure that we
    don't actually kill the child (OS-level) process before it has the
    chance to properly clean up.

    Note:
        We're technically polling in a loop, but it isn't actually
        *that* bad: typically ``.terminate()`` is only called when we're
        on the bad path (e.g. the parallel workload errored out), and
        after the performance-critical part of the code (said workload).
    """
    try:
        cd, timeout, on_timeout = _PollerArgs.from_config(cache._config_source)
        if on_timeout not in ('ignore', 'warn', 'error'):
            on_timeout = _PollerArgs.get_defaults().on_timeout
        # `_process_has_returned()` takes a `timeout` which it passes to
        # `popen.wait()`; said timeout is essentially a limit as to how
        # often the function is called, hence our cooldown
        poller = _Poller.poll_until(_process_has_returned, self, cache, cd)
        with poller.with_timeout(timeout, cast(_OnTimeout, on_timeout)):
            pass
    except _Poller.Timeout as e:  # Also handles `~.TimeoutWarning`
        cache._debug_output(f'{type(e).__qualname__}: {e}')
        raise
    finally:  # Always call `Process.terminate()` to avoid orphans
        vanilla_impl(self)


@LineProfilingCache._method_wrapper  # nocover
def wrap_bootstrap(
    cache: LineProfilingCache,
    vanilla_impl: Callable[Concatenate[BaseProcess, PS], T],
    self: BaseProcess,
    /,
    *args: PS.args, **kwargs: PS.kwargs
) -> T:
    """
    Wrap around :py:meth:`BaseProcess._bootstrap` to run
    ``LineProfilingCache.load().cleanup()`` so that profiling results
    can be gathered.

    Notes:

        - This is only invoked in child processes, and
          :py:mod:`coverage` seems to be having trouble with them in the
          current setup, probably due to issues with .pth file
          precendence causing :py:mod:`line_profiler` to be loaded
          before it. Hence the ``# nocover``.

        - ``SIGTERM`` handling is not consistent on Windows, so we made
          :py:meth:`LineProfilingCache._add_signal_handler` a no-op
          there. Hence :py:func:`wrap_terminate` remains necessary in
          mitigating unclean exits.
    """
    # Set a signal handler for SIGTERM to help child processes with
    # consistently cleaning up
    if _get_config(cache._config_source)['catch_sigterm']:
        cache._add_signal_handler()
    try:
        return vanilla_impl(self, *args, **kwargs)
    finally:
        msg = 'Calling cleanup hook via `BaseProcess._bootstrap`'
        cache._debug_output(msg)
        # Execute cleanup in a separate thread so as to avoid deadlocks,
        # in case when `LineProfilingCache._handle_signal()` caught a
        # signal as we're in the middle of this and initiated another
        # `.cleanup()` call
        cache.cleanup(new_thread=True)


# --------------- `multiprocessing.util` logging patches ---------------


def _cache_hook(
    vanilla_impl: Callable[PS, T],
    get_logging_message: Callable[PS, str],
    /,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> T:
    msg = get_logging_message(*args, **kwargs)
    LineProfilingCache.load()._debug_output(msg)
    return vanilla_impl(*args, **kwargs)


def tee_log(
    marker: str,
    vanilla_impl: Callable[Concatenate[str, PS], None],
    /,
    msg: str,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> None:
    """
    Wrap around logging functions like
    :py:func:`multiprocessing.util.debug` so that we can tee log
    messages from the package to our own logs.
    """
    def get_msg(msg: str, *_, **__) -> str:
        return f'`multiprocessing` logging ({marker}): {msg}'

    _cache_hook(
        vanilla_impl, get_msg,  # type: ignore[arg-type]
        msg, *args, **kwargs,
    )


# -------------------------- Applying patches --------------------------


_PATCHES: dict[_PatchName, Patch] = {
    'process': Patch('process').add_target(
        'BaseProcess',
        {'terminate': wrap_terminate, '_bootstrap': wrap_bootstrap},
    ),
    'pool': Patch('pool').add_target(
        'Pool', {
            # `._get_task()` is a static method, so the wrapper function
            # needs additional wrapping
            '_get_tasks': [
                attrgetter('__func__'), wrap_get_tasks, staticmethod,
            ],
            '_guarded_task_generation': wrap_guarded_task_generation,
        },
    ),
    'logging': Patch('util').add_target(
        # The logging functions exists directly in the module namespace
        # so no further attribute access is needed
        '', {func: partial(partial, tee_log, func) for func in _LOGGERS},
    ),
}


def _stop_forkserver() -> None:
    """
    Note:
        This uses `ForkServer._stop()` which is private API, but it's
        the same hack used in Python's own test suite -- see the comment
        to said method
    """
    # Appease the type-checker since `._stop()` is not public API
    stop = getattr(forkserver._forkserver, '_stop', None)
    assert callable(stop)
    stop()


def apply(
    lp_cache: LineProfilingCache,
    reboot_forkserver: bool = True,
    patches: Collection[_PatchName] | None = None,
) -> None:
    """
    Set up profiling in :py:mod:`multiprocessing` child processes by
    applying patches to the module.

    Args:
        lp_cache (LineProfilingCache):
            Cache instance governing the profiling run.
        reboot_forkserver (bool):
            Whether to reboot the global
            :py:class`multiprocessing.forkserver.ForkServer` instance
            so as to ensure that profiling happens on processes forked
            therefrom (see Note).
        patches \
(Collection[Literal['pool', 'process', 'logging'] | None]):
            Patches to apply to :py:mod:`multiprocessing`; see the
            following section for a description of each;
            the default is taken from the TOML config file.

    Patches:
        ``'pool'``:
            Patch :py:class:`multiprocessing.pool.Pool`'s
            ``._get_tasks()`` and ``._guarded_task_generation()``
            methods so that parallel tasks write profiling output.
        ``'process'``:
            Patch :py:class:`multiprocessing.process.BaseProcess`'s
            ``.terminate()`` and ``._bootstrap()`` methods so that child
            processes write profiling output on exit and are given
            enough time for that.
        ``'logging'``:
            Patch :py:mod:`multiprocessing.util`'s logging methods (e.g.
            ``debug()`` and ``info()``) so that their messages are teed
            to the cache's debug log.

    Side effects:
        - The aforementioned patches applied

        - If ``reboot_forkserver=True``, fork-server process rebooted:

          - Immediately

          - When ``lp_cache.cleanup()`` is run

        - Cleanup callbacks registered via ``lp_cache.add_cleanup()``

    Note:
        Rebooting the fork server is necessary because its process
        staticly inherits the environment when it is first spun up
        (see :py:func:`multiprocessing.forkserver.ensure_running`).
        Thus, without the reboots:

        - If in the same Python process we ever start up two separate
          profliing sessions managed by different caches, the child
          processes forked from the server will fail to inherit the
          updated environment variables injected by the newer cache
          instance, leading to the setup code in this subpackage not
          being loaded.

        - Since 3.13.8 and 3.14.1, the bug where the ``main_path``
          argument to :py:func:`multiprocessing.forkserver.main` is
          unused has been fixed (see ``cpython`` issue `GH-126631`_).
          This causes ``sys.modules['__main__']`` to be set up in the
          fork-server process, meaning that children forked therefrom
          will NOT redo the setup. Thus, the fork-server process itself
          will also need to be properly set up for profiling.

    .. _GH-126631: https://github.com/python/cpython/issues/126631
    """
    if getattr(multiprocessing, _PATCHED_MARKER, False):
        return
    if patches is None:
        config = _get_config(lp_cache._config_source)['patches']
        patches_ = {patch for patch, applied in config.items() if applied}
    else:
        patches_ = {p.lower() for p in patches}
    # Patch `multiprocessing.spawn`
    if _CAN_USE_SPAWN and hasattr(spawn, 'runpy'):
        lp_cache.patch(spawn, 'runpy', create_runpy_wrapper(lp_cache))
    # Patch methods/functions in these entities:
    # - `multiprocessing.pool.Pool`
    # - `multiprocessing.process.BaseProcess`
    # - `multiprocessing.util`
    for name, patch in _PATCHES.items():
        if name in patches_:
            patch.apply(lp_cache)
    # Stop the current `ForkServer` server process:
    # - Now, so that the (rebooted) fork-server process has profiling
    #   set up; and
    # - Also as a part of cache cleanup
    if _CAN_USE_FORKSERVER and reboot_forkserver:
        _stop_forkserver()
        lp_cache.add_cleanup(_stop_forkserver)
    # Mark `multiprocessing` as having been patched
    lp_cache.patch(multiprocessing, _PATCHED_MARKER, True)
