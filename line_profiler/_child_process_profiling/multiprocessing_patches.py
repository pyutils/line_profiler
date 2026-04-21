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


class PickleHook:
    """
    Object which, when unpickled, sets up profiling in the
    :py:mod:`multiprocessing`-created process.

    See also:
        :py:class:`coverage.multiproc.Stowaway`
    """
    def __getstate__(_) -> int:
        # Cannot return `None`, or nothing will be pickled and
        # `.__getstate__()` will not be invoked in the child
        return 1

    def __setstate__(*_) -> None:
        # We're in a child process created by `multiprocessing`, so set
        # up shop here.
        lp_cache = LineProfilingCache.load()
        lp_cache._setup_in_child_process(False, 'multiprocessing')
        if not getattr(multiprocessing, _PATCHED_MARKER, False):
            _apply_mp_patches(lp_cache, main_process=False)


class _Poller:
    """
    Poll a callable until it returns true-y.

    Example:
        >>> from itertools import count
        >>> from typing import Iterator
        >>>
        >>>
        >>> def count_until(limit: int) -> bool:
        ...     def counter_is_big_enough(
        ...         counter: Iterator[int], limit: int,
        ...     ) -> bool:
        ...         return next(counter) >= limit
        ...
        ...     return _Poller.poll_until(
        ...         counter_is_big_enough, count(), limit,
        ...     )
        ...
        >>>
        >>> with count_until(10).with_cooldown(.01).with_timeout(.25):
        ...     print('We counted up to 10')
        We counted up to 10
        >>> with count_until(30).with_cooldown(.01).with_timeout(.25):
        ...     print('We counted up to 30')  \
# doctest: +NORMALIZE_WHITESPACE
        Traceback (most recent call last):
          ...
        line_profiler..._Poller.Timeout: ...
        timed out (... s >= 0.25 s) waiting for
        callback ...counter_is_big_enough... to return true
    """
    def __init__(
        self,
        func: Callable[[], Any],
        cooldown: float = 0,
        timeout: float = 0,
        on_timeout: _OnTimeout = 'error',
    ) -> None:
        if cooldown < 0:
            cooldown = 0
        if timeout < 0:
            timeout = 0
        self._func: Callable[[], Any] = func
        self._cooldown = cooldown
        self._timeout = timeout
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
            warnings.warn(msg)
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
        .get_subconfig('multiprocessing', copy=True)
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
    finally:  # Always call `Process.terminate()` to avoid orphans
        vanilla_impl(self)


@LineProfilingCache._method_wrapper
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
    """
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
    vanilla_impl: Callable[Concatenate[str, PS], None],
    marker: str,
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


@LineProfilingCache._method_wrapper
def wrap_get_preparation_data(
    # We don't use the cache here, but
    # `@LineProfilingCache._method_wrapper` expects it in the signature
    # (and we want the debug output)
    _,
    vanilla_impl: Callable[PS, dict[str, Any]],
    /,
    *args: PS.args,
    **kwargs: PS.kwargs
) -> dict[str, Any]:
    """
    Wrap around :py:func:`multiprocessing.spawn.get_preparation_data`,
    slipping a :py:class:`PickleHook` into the returned dictionary so
    that profiling is triggered upon unpickling.

    Args:
        vanilla_impl
            Vanilla
            :py:func:`multiprocessing.spawn.get_preparation_data`
        *args
        **kwargs
            Passed to
            :py:func:`multiprocessing.spawn.get_preparation_data`

    Returns
        Dictionary returned by
        ``get_preparation_data(*args, **kwargs)`` with an extra key
    """
    key = 'line_profiler_pickle_hook'  # Doesn't matter
    data = vanilla_impl(*args, **kwargs)
    assert key not in data
    data[key] = PickleHook()
    return data


def apply(lp_cache: LineProfilingCache) -> None:
    """
    Set up profiling in :py:mod:`multiprocessing` child processes by
    applying patches to the module.

    Args:
        lp_cache (LineProfilingCache)
            Cache instance governing the profiling run

    Side effects:
        - :py:mod:`multiprocessing` marked as having been set up

        - The following methods and functions patched:

          - :py:meth:`multiprocessing.process.BaseProcess.terminate`

          - :py:meth:`multiprocessing.process.BaseProcess._bootstrap`

          - :py:func:`multiprocessing.spawn.get_preparation_data`

        - Cleanup callbacks registered via ``lp_cache.add_cleanup()``

    Note:
        When ``lp_cache.cleanup()`` is run, the global
        :py:class:`multiprocessing.forkserver.ForkServer` object will be
        rebooted. This is necessary because the server process staticly
        inherits the environment when it is first spun up
        (see :py:func:`multiprocessing.forkserver.ensure_running`).
        Thus, if in the same Python process we ever start up two
        separate profliing sessions managed by different caches, the
        child processes forked from the server will fail to inherit the
        updated environment variables injected by the newer cache
        instance, leading to the setup code in this subpackage not being
        loaded.
    """
    if not getattr(multiprocessing, _PATCHED_MARKER, False):
        _apply_mp_patches(lp_cache)


def _apply_mp_patches(
    lp_cache: LineProfilingCache,
    main_process: bool = True,
    debug: bool | None = None,
) -> None:
    # In a child process, we don't care about polluting the
    # `multiprocessing` namespace, so don't bother with cleanup
    replace = partial(lp_cache.patch, cleanup=main_process)

    # Patch `multiprocessing.process.BaseProcess` methods
    # Note: the type checkers seem to need some help figuring the
    # `patches` out... so do explicit `cast()`s
    for submodule, target, patches in [
        ('process', 'BaseProcess', {
            'terminate': cast(_Wrapper[..., None], wrap_terminate),
            '_bootstrap': cast(_Wrapper[..., Any], wrap_bootstrap),
        }),
    ]:
        try:
            mod = import_module('multiprocessing.' + submodule)
        except ImportError:
            continue
        Class = getattr(mod, target)
        name = f'{Class.__module__}.{Class.__qualname__}'
        patch_class = partial(replace, Class, name=name)
        for method, method_wrapper in patches.items():
            vanilla = getattr(Class, method)
            patch_class(method, method_wrapper(vanilla))

    # Patch `multiprocessing.spawn`
    try:
        from multiprocessing import spawn
    except ImportError:  # Incompatible platforms
        pass
    else:
        patch_spawn = partial(replace, spawn, name=spawn.__name__)
        # Patch `get_preparation_data()`
        gpd_wrapper = wrap_get_preparation_data(spawn.get_preparation_data)
        patch_spawn('get_preparation_data', gpd_wrapper)
        # Patch `runpy` (do it locally instead of tempering with the
        # global `runpy` mmodule)
        if hasattr(spawn, 'runpy'):
            runpy_wrapper = create_runpy_wrapper(lp_cache)
            patch_spawn('runpy', runpy_wrapper)

    # Intercept `multiprocessing` debug messages
    if debug is None:
        debug = _get_config(lp_cache.config)['intercept_logs']
    if debug:
        from multiprocessing import util

        patch_util = partial(replace, util, name=util.__name__)
        for logging_func in [
            'sub_debug', 'debug', 'info', 'sub_warning', 'warn',
        ]:
            try:
                vanilla = getattr(util, logging_func)
            except AttributeError:
                continue
            patch_util(logging_func, partial(tee_log, vanilla, logging_func))

    # Stop the current `ForkServer` server process as a part of cache
    # cleanup (this uses `ForkServer._stop()` which is private API, but
    # it's the same hack used in Python's own test suite -- see the
    # comment to said method)
    if main_process:
        try:
            from multiprocessing import forkserver
        except ImportError:  # Incompatible platform
            pass
        else:
            server_instance: forkserver.ForkServer = forkserver._forkserver
            stop = getattr(server_instance, '_stop', None)
            assert callable(stop)  # Appease the type checker
            lp_cache.add_cleanup(stop)

    # Mark `multiprocessing` as having been patched
    replace(multiprocessing, _PATCHED_MARKER, True, name='multiprocessing')


def _no_op(*_, **__) -> None:
    pass
