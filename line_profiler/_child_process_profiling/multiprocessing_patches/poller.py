from __future__ import annotations

import warnings
from collections.abc import Callable
from functools import partial
from time import sleep, monotonic
from typing import Any, Literal, NoReturn
from typing_extensions import ParamSpec, Self

from ... import _diagnostics as diagnostics


__all__ = ('Poller', 'OnTimeout')

PS = ParamSpec('PS')
OnTimeout = Literal['ignore', 'warn', 'error']


class Poller:
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
        ...             Poller.poll_until, counter_is_big_enough,
        ...         )
        ...     else:
        ...         get_poller = partial(
        ...             Poller.poll_while, counter_is_small_enough,
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
        line_profiler...Poller.Timeout: ...
        timed out (... s >= 0.25 s) waiting for
        callback ...counter_is_big_enough... to return true

        >>> with ExitStack() as stack:  # doctest: +NORMALIZE_WHITESPACE
        ...     enter = stack.enter_context
        ...     enter(warnings.catch_warnings())
        ...     warnings.simplefilter('error', Poller.TimeoutWarning)
        ...     enter(
        ...         count_until(30, 'while')
        ...         .with_cooldown(.01)
        ...         .with_timeout(.25, 'warn')
        ...     )
        ...     print('We counted up to 30 again')
        Traceback (most recent call last):
          ...
        line_profiler...Poller.TimeoutWarning: ...
        timed out (... s >= 0.25 s) waiting for
        callback ...counter_is_small_enough... to return true
    """
    def __init__(
        self,
        func: Callable[[], Any],
        cooldown: float = 0,
        timeout: float = 0,
        on_timeout: OnTimeout = 'error',
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
        on_timeout: OnTimeout | None = None,
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

        def no_op(_) -> None:
            pass

        timeout = self._timeout
        callback = self._func

        handle_timeout: Callable[[str], Any] = {
            'error': error, 'warn': warn, 'ignore': no_op,
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
        Raised when a :py:class:`Poller` is timed out when polling.
        """
        pass

    class TimeoutWarning(Timeout, UserWarning):
        """
        Issued when a :py:class:`Poller` is timed out when polling.
        """
        pass
