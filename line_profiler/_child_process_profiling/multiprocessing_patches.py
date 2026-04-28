"""
Patch :py:mod:`multiprocessing` so that profiling extends into processes
it creates.

Notes
-----
- Based on the implementations in :py:mod:`coverage.multiproc` and
  :py:mod:`pytest_autoprofile._multiprocessing`.
- Results may vary if the process pool is not properly
  :py:meth:`multiprocessing.pool.Pool.close`-d and
  :py:meth:`multiprocessing.pool.Pool.join`-ed;
  see `this caveat <https://coverage.readthedocs.io/\
en/latest/subprocess.html#using-multiprocessing>`__.
"""
from __future__ import annotations

import multiprocessing
import warnings
from collections.abc import Callable, Mapping
from functools import lru_cache, partial
from importlib import import_module
from multiprocessing.process import BaseProcess
from os import PathLike
from time import sleep, monotonic
from types import MappingProxyType
from typing import (
    Any, Generic, Literal, Protocol, TypeVar, Union, NoReturn, cast,
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
PS = ParamSpec('PS')
_OnTimeout = Literal['ignore', 'warn', 'error']

_PATCHED_MARKER = '__line_profiler_patched_multiprocessing__'


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
            warnings.warn(msg, type(self).TimeoutWarning, stacklevel=3)
            diagnostics.log.warning(msg)

        def ignore(_):
            pass

        timeout = self._timeout
        callback = self._func

        handle_timeout: Callable[[str], Any] = {
            'error': error, 'warn': warn, 'ignore': ignore,
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


def _get_config(
    config: PathLike[str] | str | bool | None = None,
) -> Mapping[str, Any]:
    if config not in (True, False, None):
        config = str(config)
    return _get_config_cached(cast(Union[str, bool, None], config))


@lru_cache()
def _get_config_cached(
    config: PathLike[str] | str | bool | None = None,
) -> Mapping[str, Any]:
    cd = dict(
        ConfigSource.from_config(config)
        .get_subconfig('child_processes', 'multiprocessing', copy=True)
        .conf_dict
    )
    assert isinstance(cd.get('polling'), Mapping)
    return MappingProxyType({**cd, 'polling': MappingProxyType(cd['polling'])})


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
    # XXX: why can `coverage` get away with not doing all these
    # lock-file hijinks and just patching `BaseProcess._bootstrap()`?
    def get_poller_args(
        config: PathLike[str] | str | bool | None = None,
    ) -> tuple[float, float, str | None]:
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
        return cooldown, timeout, on_timeout

    def process_has_returned(proc: BaseProcess, timeout: float) -> bool:
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

    def wait_for_return(
        config: PathLike[str] | str | None = None,
    ) -> _Poller:
        cooldown, timeout, on_timeout = get_poller_args(config)
        # `False` -> no resolution, force loading the vanilla file
        *_, default_on_timeout = get_poller_args(False)
        if on_timeout not in ('ignore', 'warn', 'error'):
            on_timeout = default_on_timeout
        return (
            _Poller.poll_until(process_has_returned, self, cooldown)
            .with_timeout(timeout, cast(_OnTimeout, on_timeout))
        )

    try:
        with wait_for_return(cache.config):
            pass
    except (_Poller.Timeout, _Poller.TimeoutWarning) as e:
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

    Note:
        This is only invoked in child processes, and :py:mod:`coverage`
        seem to be having trouble with them in the current setup,
        probably due to issues with .pth file precendence causing
        :py:mod:`line_profiler` to be loaded before it. Hence the
        ``# nocover``.
    """
    # Set a signal handler for SIGTERM to help child processes with
    # consistently cleaning up
    if _get_config(cache.config)['catch_sigterm']:
        cache._wrap_sigterm()
    try:
        return vanilla_impl(self, *args, **kwargs)
    finally:
        msg = 'Calling cleanup hook via `BaseProcess._bootstrap`'
        cache._debug_output(msg)
        cache.cleanup()


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


def apply(
    lp_cache: LineProfilingCache, reboot_forkserver: bool = True,
) -> None:
    """
    Set up profiling in :py:mod:`multiprocessing` child processes by
    applying patches to the module.

    Args:
        lp_cache (LineProfilingCache):
            Cache instance governing the profiling run
        reboot_forkserver (bool):
            Whether to reboot the global
            :py:class`multiprocessing.forkserver.ForkServer` instance
            so as to ensure that profiling happens on processes forked
            therefrom (see Note)

    Side effects:
        - :py:mod:`multiprocessing` marked as having been set up

        - The following methods and functions patched:

          - :py:meth:`multiprocessing.process.BaseProcess.terminate`

          - :py:meth:`multiprocessing.process.BaseProcess._bootstrap`

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
    if not getattr(multiprocessing, _PATCHED_MARKER, False):
        _apply_mp_patches(lp_cache, reboot_forkserver=reboot_forkserver)


def _apply_patches_generic(
    lp_cache: LineProfilingCache,
    submodule: str,
    targets: Mapping[str, Mapping[str, Callable[[Any], Any]]],
    cleanup: bool = True,
) -> None:
    submod_name = 'multiprocessing.' + submodule
    try:
        mod = import_module(submod_name)
    except ImportError:  # nocover
        return
    for target, patches in targets.items():
        if target:
            try:
                obj: Any = getattr(mod, target)
            except AttributeError:  # nocover
                continue
            name = f'{submod_name}.{target}'
        else:
            obj, name = mod, submod_name
        replace = partial(lp_cache.patch, obj, cleanup=cleanup, name=name)
        for method, method_wrapper in patches.items():
            try:
                vanilla = getattr(obj, method)
            except AttributeError:
                continue
            replace(method, method_wrapper(vanilla))


def _apply_mp_patches(
    lp_cache: LineProfilingCache,
    reboot_forkserver: bool = True,
    intercept_mp_logs: bool | None = None,
) -> None:
    # In a child process, we don't care about polluting the
    # `multiprocessing` namespace, so don't bother with cleanup
    apply_patches = partial(_apply_patches_generic, lp_cache)
    # Patch `multiprocessing.process.BaseProcess` methods
    # Note: the type checkers seem to need some help figuring the
    # `patches` out... so do explicit `cast()`s
    apply_patches(
        'process',
        {'BaseProcess': {'terminate': wrap_terminate,
                         '_bootstrap': wrap_bootstrap}},
    )
    # Patch `multiprocessing.spawn`
    if _CAN_USE_SPAWN and hasattr(spawn, 'runpy'):
        lp_cache.patch(spawn, 'runpy', create_runpy_wrapper(lp_cache))
    # Intercept `multiprocessing` debug messages
    if intercept_mp_logs is None:
        intercept_mp_logs = _get_config(lp_cache.config)['intercept_logs']
    if intercept_mp_logs:
        lfuncs = ['sub_debug', 'debug', 'info', 'sub_warning', 'warn']
        lpatches = {func: partial(partial, tee_log, func) for func in lfuncs}
        apply_patches('util', {'': lpatches})
    # Stop the current `ForkServer` server process:
    # - Now, so that the (rebooted) fork-server process has profiling
    #   set up; and
    # - Also as a part of cache cleanup
    if _CAN_USE_FORKSERVER and reboot_forkserver:
        _stop_forkserver()
        lp_cache.add_cleanup(_stop_forkserver)
    # Mark `multiprocessing` as having been patched
    lp_cache.patch(multiprocessing, _PATCHED_MARKER, True)


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


def _no_op(*_, **__) -> None:
    pass
