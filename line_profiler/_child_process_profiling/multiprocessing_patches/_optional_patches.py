from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TypeVar
from typing_extensions import Concatenate, ParamSpec

from ..cache import LineProfilingCache
from ._infrastructure import SingleModulePatch


__all__ = ('LOGGING_PATCH', 'tee_log')

T = TypeVar('T')
PS = ParamSpec('PS')

_LOGGERS = ['sub_debug', 'debug', 'info', 'sub_warning', 'warn']

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


LOGGING_PATCH = SingleModulePatch('util').add_target(
    # The logging functions exists directly in the module namespace so
    # no further attribute access is needed
    '', {func: partial(partial, tee_log, func) for func in _LOGGERS},
)
