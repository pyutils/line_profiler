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
import sys
import warnings
from collections.abc import Callable, Collection, Mapping, Sequence, Set
from functools import partial
from importlib import import_module
from inspect import getattr_static, signature
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess
from multiprocessing.pool import Pool
from operator import attrgetter
from time import sleep, monotonic
from types import MappingProxyType as mappingproxy, MethodType, ModuleType
from typing import (
    TYPE_CHECKING,
    Any, ClassVar, Generic, Literal, NamedTuple, Protocol, TypeVar, NoReturn,
    cast, final, overload,
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
try:
    from multiprocessing import resource_tracker
except ImportError:
    _CAN_USE_RESOURCE_TRACKER = False
else:
    _CAN_USE_RESOURCE_TRACKER = True

from .. import _diagnostics as diagnostics
from ..toml_config import ConfigSource
from .cache import LineProfilingCache
from .runpy_patches import create_runpy_wrapper


__all__ = ('apply',)


T = TypeVar('T')
T1 = TypeVar('T1')
T2 = TypeVar('T2')
P = TypeVar('P', bound=BaseProcess)
Pt = TypeVar('Pt', bound='_Patch')
PS = ParamSpec('PS')
PS1 = ParamSpec('PS1')
PS2 = ParamSpec('PS2')
_OnTimeout = Literal['ignore', 'warn', 'error']
PublicPatch = Literal['pool', 'process', 'logging']

_PATCHED_MARKER = '__line_profiler_patched_multiprocessing__'
_LOGGERS = ['sub_debug', 'debug', 'info', 'sub_warning', 'warn']
_PATCHES: dict[str, '_Patch'] = {}


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
    def new(cls, cooldown: Any, timeout: Any, on_timeout: Any) -> Self:
        try:
            cd = max(float(cooldown), 0)
        except (TypeError, ValueError):
            cd = 0
        try:
            to = max(float(timeout), 0)
        except (TypeError, ValueError):
            to = 0
        try:
            ot: str | None = on_timeout.lower()
        except Exception:  # Fallback (use `_Poller`'s default)
            ot = None
        return cls(cd, to, ot)


@final
@dataclasses.dataclass
class MPConfig:
    """
    Consolidate the config options into a structured object.
    """
    catch_sigterm: bool
    patches: dict[PublicPatch, bool]
    polling: _PollerArgs

    def _get_terminate_poller(
        self, cache: LineProfilingCache, process: BaseProcess,
    ) -> _Poller:
        cd, timeout, on_timeout = self.polling
        if on_timeout not in ('ignore', 'warn', 'error'):
            on_timeout = self.get_defaults().polling.on_timeout
        # `_process_has_returned()` takes a `timeout` which it passes to
        # `popen.wait()`; said timeout is essentially a limit as to how
        # often the function is called, hence our cooldown
        poller = _Poller.poll_until(
            self._process_has_returned, process, cache, cd,
        )
        return poller.with_timeout(timeout, cast(_OnTimeout, on_timeout))

    @classmethod
    def from_config(cls, config: ConfigSource) -> Self:
        loaded = (
            config
            .get_subconfig('child_processes', 'multiprocessing')
            .conf_dict
        )
        polling = _PollerArgs.new(**loaded['polling'])
        return cls(
            catch_sigterm=loaded['catch_sigterm'],
            patches=dict(loaded['patches']),
            polling=polling,
        )

    @classmethod
    def from_cache(cls, cache: LineProfilingCache) -> Self:
        key = 'mp_config'
        try:
            return cache._additional_data[key]
        except KeyError:
            config = cls.from_config(cache._config_source)
            return cache._additional_data.setdefault(key, config)

    @classmethod
    def get_defaults(cls) -> Self:
        namespace = globals()
        name = '_DEFAULT_CONFIG'
        try:
            return namespace[name]
        except KeyError:
            defaults = cls.from_config(ConfigSource.from_default(copy=False))
            return namespace.setdefault(name, defaults)

    @staticmethod
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


def _no_op(*_, **__) -> None:
    pass


# ---------------------- Patching infrastructure -----------------------


class _Patch(Protocol):
    """
    Interface for patches.
    """
    def apply(
        self,
        cache: LineProfilingCache,
        *,
        cleanup: bool = True,
        **kwargs
    ) -> Any:
        """
        Apply the patch.

        Args:
            cache (LineProfilingCache):
                Session cache
            cleanup (bool):
                Whether ``cache.cleanup()`` should reverse the patch
            **kwargs
                Individual implementations should pick the ones they
                need and ignore the rest.
        """
        ...

    @property
    def summary(self) -> Mapping[str, Set[str]]:
        """
        A mapping from dotted-path names of objects to the set of
        attributes patched thereon.
        """
        ...


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

        Args:
            target (str):
                Dotted path to the object in :py:attr:`.submodule`
            patches (Mapping[str, Callable[[Any], Any] \
| Sequence[Callable[[Any], Any]]]):
                Mapping from patched attrbute names to the wrappers to
                apply thereto; sequences of wrappers are applied in
                order

        Returns:
            This instance
        """
        self.targets.setdefault(target, {}).update(patches)
        return self

    def add_method(
        self,
        target: str,
        method: str,
        wrapper: Callable[[Any], Any],
        methodtype: (
            type[classmethod] | type[staticmethod]
            | Literal['class', 'static'] | None
        ) = None,
    ) -> Self:
        """
        Convenience method for gradually constructing the patch with a
        fluent interface.

        Args:
            target (str):
                Dotted path to the object in :py:attr:`.submodule`
            method (str):
                Name of the (class, static, or instance) method to patch
            wrapper (Callable[[Any], Any]):
                Wrapping callable which takes the method-implementaion
                callable and returns a wrapper thereof
            methodtype (type[classmethod] | type[staticmethod] | \
Literal['class', 'static'] | None):
                Optional type of the method if not an instance method;
                the strings ``'class'`` and ``'static'`` are respective
                shorthands for :py:class:`classmethod` and
                :py:class:`staticmethod`

        Returns:
            This instance
        """
        wrappers: Callable[[Any], Any] | list[Callable[[Any], Any]]
        if methodtype is None:
            wrappers = wrapper
        else:
            if methodtype == 'class':
                methodtype = classmethod
            elif methodtype == 'static':
                methodtype = staticmethod
            wrappers = [attrgetter('__func__'), wrapper, methodtype]
        return self.add_target(target, {method: wrappers})

    def apply(
        self,
        cache: LineProfilingCache,
        *,
        cleanup: bool = True,
        static: bool = True,
        **_
    ) -> list[str]:
        """
        Apply the patch.

        Args:
            cache (LineProfilingCache):
                Session cache
            cleanup (bool):
                Whether ``cache.cleanup()`` should reverse the patch
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
    def summary(self) -> mappingproxy[str, frozenset[str]]:
        """
        Summary of the dotted paths to the patched objects and their
        patched attributes
        """
        add_prefix = partial(self._join, self.module)
        return mappingproxy({
            add_prefix(target): frozenset(patches)
            for target, patches in self.targets.items()
        })


@overload
def _register_patch(name: str, patch: Pt) -> Pt:
    ...


@overload
def _register_patch(name: str, patch: None = None) -> _Patch:
    ...


def _register_patch(name: str, patch: _Patch | None = None) -> _Patch:
    """
    Register the ``patch`` under ``name`` and return it as-is. If
    ``patch`` isn't provided, look for the existing patch registered
    under the name.

    Note:
        Patches named with leading double underscores are applied no
        matter the user input (e.g. via ``apply(..., patches=...)`` or
        the config file).
    """
    if patch is not None:
        if _PATCHES.setdefault(name, patch) is not patch:
            raise ValueError(
                f'name = {name!r}, patch = {patch!r}: '
                'name already in use by {_PATCHES[name]}'
            )
    return _PATCHES[name]


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
        :py:meth:`.Pool._get_tasks` is a static method.
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


@LineProfilingCache._method_wrapper
def wrap_repopulate_pool_static(
    cache: LineProfilingCache,
    vanilla_impl: Callable[
        Concatenate[BaseContext, type[P], int, list[P], PS], None
    ],
    ctx: BaseContext,
    Process: type[P],
    processes: int,
    pool: list[P],
    *args: PS.args,
    **kwargs: PS.kwargs
) -> None:
    """
    Wrap around :py:meth:`.Pool._repopulate_pool_static` so that we can
    keep track of the PIDs of the created child processes.

    Note:
        :py:meth:`.Pool._repopulate_pool_static` is a static method.
    """
    try:
        vanilla_impl(ctx, Process, processes, pool, *args, **kwargs)
    finally:
        patches = MPConfig.from_cache(cache).patches
        if not patches.get('process', False):
            # Notes:
            # - if the `process` patch is active, child processes should
            #   always have called `.cache.profiler.dump_stats()` at
            #   least once before exiting, so there's no need to warn
            #   the cache against children possibly not having written
            #   profiling data.
            # - Since the vanilla implementation calls `Process.start()`
            #   on each child, they should all have valid PIDs. However:
            #   - The process might have terminated for whatever reason.
            #     and
            #   - `multiprocessing.dummy.DummyProcess` doesn't have
            #     `.pid`.
            #   So we add a fallback to `None` just in case.
            pids = cast(
                set[int],
                {getattr(process, 'pid', None) for process in pool} - {None},
            )
            cache._warn_possible_lack_of_stats(pids)


_patch_pool = partial(
    _register_patch('pool', Patch('pool')).add_method, 'Pool',
)
_patch_pool('_get_tasks', wrap_get_tasks, 'static')
_patch_pool('_guarded_task_generation', wrap_guarded_task_generation)
_patch_pool('_repopulate_pool_static', wrap_repopulate_pool_static, 'static')

# ----------- `multiprocessing.process.BaseProcess` patches ------------


@LineProfilingCache._method_wrapper
def wrap_terminate(
    cache: LineProfilingCache,
    vanilla_impl: Callable[[BaseProcess], None],
    self: BaseProcess,
) -> None:
    """
    Wrap around :py:meth:`.BaseProcess.terminate` to make sure that we
    don't actually kill the child (OS-level) process before it has the
    chance to properly clean up.

    Note:
        We're technically polling in a loop, but it isn't actually
        *that* bad: typically ``.terminate()`` is only called when we're
        on the bad path (e.g. the parallel workload errored out), and
        after the performance-critical part of the code (said workload).
    """
    try:
        config = MPConfig.from_cache(cache)
        with config._get_terminate_poller(cache, self):
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
    Wrap around :py:meth:`.BaseProcess._bootstrap` to run
    ``LineProfilingCache.load().cleanup()`` so that profiling results
    can be gathered.

    Notes:

        - This is only invoked in child processes, and
          :py:mod:`coverage` seems to be having trouble with them in the
          current setup, probably due to issues with .pth file
          precendence causing :py:mod:`line_profiler` to be loaded
          before it. Hence the ``# nocover``.

        - ``SIGTERM`` handling is not consistent on Windows, so we made
          :py:meth:`.LineProfilingCache._add_signal_handler` a no-op
          there. Hence :py:func:`wrap_terminate` remains necessary in
          mitigating unclean exits.
    """
    # Set a signal handler for SIGTERM to help child processes with
    # consistently cleaning up
    if MPConfig.from_cache(cache).catch_sigterm:
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


_patch_process = partial(
    _register_patch('process', Patch('process')).add_method, 'BaseProcess',
)
_patch_process('_bootstrap', wrap_bootstrap)
# We only need to patch `Process.terminate()` if we can't do SIGTERM
# handling, i.e. on Windows
if sys.platform == 'win32':
    _patch_process('terminate', wrap_terminate)

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


_register_patch('logging', Patch('util')).add_target(
    # The logging functions exists directly in the module namespace so
    # no further attribute access is needed
    '', {func: partial(partial, tee_log, func) for func in _LOGGERS},
)

# --------------------------- Misc. patches ----------------------------


class RebootForkserverPatch:
    """
    Reboot the process backing the global
    :py:class:`multiprocessing.forkserver.ForkServer` instance:

    - When the patch is applied, so as to ensure that child processes
      forked therefrom actually receives the active patches; and

    - When the session cache is cleaned up, so that child processes
      forked therefrom is no longer polluted by the patches.

    Note:
        This uses
        :py:method:`multiprocessing.forkserver.ForkServer._stop()` which
        is private API, but it's the same hack used in Python's own test
        suite -- see the comment to said method.
    """
    summary: ClassVar[mappingproxy[str, frozenset[str]]] = mappingproxy({})

    @classmethod
    def apply(cls, cache: LineProfilingCache, **_) -> None:
        if not _CAN_USE_FORKSERVER:
            return
        cls.reboot()
        cache.add_cleanup(cls.reboot)

    @staticmethod
    def reboot() -> None:
        # Appease the type-checker since `._stop()` is not public API
        stop = getattr(forkserver._forkserver, '_stop', None)
        assert callable(stop)
        stop()


class ResourceTrackerPatch:
    """
    Patch :py:mod:`multiprocessing.resource_tracker` so that
    :py:func:`multiprocessing.resource_tracker.ensure_running` and the
    eponymous method of
    :py:class:`multiprocessing.resource_tracker.ResourceTracker` report
    the resource-tracker server PIDs to the session cache.

    Note:
        The ``ResourceTracker`` server process is spawned when the first
        :py:mod:`multiprocessing` child process is created via the
        ``spawn`` or ``forkserver`` start methods. While this server
        process does not meaningfully contribute to the profiling result
        either way, since it can be created with profiling set up, its
        longevity means that :py:meth:`.LineProfilingCache.gather_stats`
        often catches empty .lprof files which it has occupied but not
        written to.

        To reduce noise while keeping the empty-file warning for other
        output files, we report the PIDs used by the server to the
        session cache so that they can be ignored if necessary.
    """
    if _CAN_USE_RESOURCE_TRACKER:
        summary: ClassVar[mappingproxy[str, frozenset[str]]] = mappingproxy({
            'multiprocessing.resource_tracker':
            frozenset({'ensure_running'}),
            'multiprocessing.resource_tracker.ResourceTracker':
            frozenset({'ensure_running'}),
        })
    else:
        summary = mappingproxy({})

    @staticmethod
    @LineProfilingCache._method_wrapper
    def wrap_ensure_running(
        cache: LineProfilingCache,
        vanilla_impl: Callable[['resource_tracker.ResourceTracker'], None],
        self: 'resource_tracker.ResourceTracker',
    ) -> None:
        """
        Wrap around :py:meth:`multiprocessing.resource_tracker\
.ResourceTracker.ensure_running`
        so that the session cache can keep track of the PIDs used by the
        resource-tracer server.
        """
        maybe_pids: set[int | None] = {getattr(self, '_pid', None)}
        try:
            vanilla_impl(self)
        finally:
            maybe_pids.add(getattr(self, '_pid', None))
            pids = cast(set[int], maybe_pids - {None})
            if pids:
                cache._warn_possible_lack_of_stats(pids)

    @classmethod
    def apply(
        cls, cache: LineProfilingCache, *, cleanup: bool = True, **_,
    ) -> list[str]:
        if _CAN_USE_RESOURCE_TRACKER:
            patch = partial(cache.patch, cleanup=cleanup)
            # Patch the method on the class
            method = resource_tracker.ResourceTracker.ensure_running
            method = cls.wrap_ensure_running(method)
            patch(resource_tracker.ResourceTracker, 'ensure_running', method)
            # Patch the preexisting bound method on the module
            instance = resource_tracker._resource_tracker
            bound_method = MethodType(method, instance)
            patch(resource_tracker, 'ensure_running', bound_method)
        return list(cls.summary)


class RunpyPatch:
    """
    Patch the copy of :py:mod:`runpy` in the
    :py:mod:`multiprocessing.spawn` namespace so that subprocesses can
    perform rewrite-based profiling as with
    :py:func:`line_profiler.autoprofile.autoprofile.run`.

    See also:
        :py:mod:`line_profiler._child_process_profiling.runpy_patches`
    """
    summary: ClassVar[mappingproxy[str, frozenset[str]]]
    if _CAN_USE_SPAWN and hasattr(spawn, 'runpy'):
        summary = mappingproxy({'multiprocessing.spawn': frozenset({'runpy'})})
    else:
        summary = mappingproxy({})

    @classmethod
    def apply(
        cls, cache: LineProfilingCache, *, cleanup: bool = True, **_,
    ) -> list[str]:
        if cls.summary:
            patch = partial(cache.patch, cleanup=cleanup)
            patch(spawn, 'runpy', create_runpy_wrapper(cache))
        return list(cls.summary)


# See `ty` issue #3429 for why we need the casts
_register_patch('__reboot_forkserver', cast(_Patch, RebootForkserverPatch))
_register_patch('__resource_tracker', cast(_Patch, ResourceTrackerPatch))
_register_patch('__spawn_runpy', cast(_Patch, RunpyPatch))

# -------------------------- Applying patches --------------------------


def apply(
    cache: LineProfilingCache,
    reboot_forkserver: bool = True,
    patches: Collection[PublicPatch] | None = None,
) -> None:
    """
    Set up profiling in :py:mod:`multiprocessing` child processes by
    applying patches to the module.

    Args:
        cache (LineProfilingCache):
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
            ``._bootstrap()`` method (and ``.terminate()`` on Windows)
            so that child processes write profiling output on exit and
            are given enough time for that.
        ``'logging'``:
            Patch :py:mod:`multiprocessing.util`'s logging methods (e.g.
            ``debug()`` and ``info()``) so that their messages are teed
            to the cache's debug log.

    Side effects:
        - The aforementioned patches applied

        - If ``reboot_forkserver=True``, fork-server process rebooted:

          - Immediately

          - When ``cache.cleanup()`` is run

        - Cleanup callbacks registered via ``cache.add_cleanup()``

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
        patches_dict = MPConfig.from_cache(cache).patches
        patches_: set[str] = {p for p, use in patches_dict.items() if use}
    else:
        patches_ = {p.lower() for p in patches}
    for name, patch in _PATCHES.items():
        if name in patches_:
            should_apply = True
        elif name.startswith('__'):
            should_apply = (name != '__reboot_forkserver' or reboot_forkserver)
        else:
            should_apply = False
        if should_apply:
            msg = f'applying `multiprocessing` patch {name!r}'
            cache._debug_output(msg.capitalize() + '...')
            patch.apply(cache)
            cache._debug_output('Done with ' + msg)
    # Mark `multiprocessing` as having been patched
    cache.patch(multiprocessing, _PATCHED_MARKER, True)
