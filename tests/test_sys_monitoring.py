import inspect
import sys
from collections import Counter
from contextlib import AbstractContextManager, ExitStack
from functools import partial
from io import StringIO
from types import CodeType, ModuleType
from typing import (Any, Optional, Union,
                    Callable, Generator,
                    Dict, FrozenSet, Tuple,
                    ClassVar)

import pytest

from line_profiler import LineProfiler
from line_profiler._line_profiler import label


USE_SYS_MONITORING = isinstance(getattr(sys, 'monitoring', None), ModuleType)


# -------------------------------------------------------------------- #
#                            Helper classes                            #
# -------------------------------------------------------------------- #


class SysMonHelper:
    """
    Helper object which helps with simplifying attribute access on
    :py:mod:`sys.monitoring`.
    """
    tool_id: int
    no_tool_id_callables: ClassVar[FrozenSet[str]] = frozenset(
        {'restart_events'})

    def __init__(self, tool_id: Optional[int] = None) -> None:
        if tool_id is None:
            tool_id = sys.monitoring.PROFILER_ID
        self.tool_id = tool_id

    def __getattr__(self, attr: str):
        """
        Returns:
            * If ``attr`` refers to a :py:mod:`sys.monitoring` callable:
              a :py:func:`functools.partial` object with
              :py:attr:`~.tool_id` pre-applied, unless it is in the set
              of explicitly excluded methods
              (:py:attr:`~.no_tool_id_callables`).
            * If ``attr`` is all uppercase:
              the corresponding integer or special (e.g.
              :py:data:`~sys.monitoring.MISSING` and
              :py:data:`~sys.monitoring.DISABLE`) constants from either
              the module or the :py:data:`sys.monitoring.events`
              namespace.
            * Otherwise, it falls through to the module.
        """
        mon = sys.monitoring
        if attr.isupper():
            try:
                return getattr(mon.events, attr)
            except AttributeError:
                pass
        result = getattr(mon, attr)
        if callable(result) and attr not in self.no_tool_id_callables:
            return partial(result, self.tool_id)
        return result


class restore_events(AbstractContextManager):
    """
    Restore the global or local :py:mod:`sys.monitoring` events.
    """
    code: Union[CodeType, None]
    mon: SysMonHelper
    events: int

    def __init__(self, *,
                 code: Optional[CodeType] = None,
                 tool_id: Optional[int] = None) -> None:
        self.code = code
        self.mon = SysMonHelper(tool_id)
        self.events = sys.monitoring.events.NO_EVENTS

    def __enter__(self):
        if self.code is None:
            self.events = self.mon.get_events()
        else:
            self.events = self.mon.get_local_events(self.code)
        return self

    def __exit__(self, *_, **__) -> None:
        if self.mon.get_tool() is None:
            pass  # No-op
        elif self.code is None:
            self.mon.set_events(self.events)
        else:
            self.mon.set_local_events(self.code, self.events)
        self.events = self.mon.NO_EVENTS


class LineCallback:
    """
    Simple :py:mod:`sys.monitoring` callback for handling LINE events.

    Attributes:
        nhits (dict[tuple[str, int, str], Counter[int]])
            Mapping from
            :py:attr:`line_profiler._line_profiler.LineStats.timings`
            keys to a :py:class:`collections.Counter` mapping line
            numbers to reported hit counts.
        predicate (Callable[[code, int], bool])
            Callable taking the code object and line number, and
            returning whether the line event should be reported.
        disable (bool)
            Settable boolean determining whether to return
            :py:data:`sys.monitoring.DISABLE` on a reported line event.
    """
    nhits: Dict[Tuple[str, int, str], 'Counter[int]']
    predicate: Callable[[CodeType, int], bool]
    disable: bool

    def __init__(
        self,
        predicate: Callable[[CodeType, int], bool],
        *,
        register: bool = True,
        disable: bool = False
    ) -> None:
        """
        Arguments:
            predicate, disable
                See attributes.
            register
                If true, register the instance with
                :py:func:`sys.monitoring.register_callback`.
        """
        self.nhits = {}
        self.predicate = predicate
        self.disable = disable
        if register:
            MON.register_callback(MON.LINE, self)

    def __call__(self, code: CodeType, lineno: int) -> Any:
        """
        Returns:
            :py:data:`sys.monitoring.DISABLE` if :py:attr:`~.predicate`
            evaluates to true AND if :py:attr:`~.disable` is true;
            :py:const:`None` otherwise.

        Side effects:
            Entry created/incremented in :py:attr:`~.nhits` if
            :py:attr:`~.predicate` evaluates to true.
        """
        if not self.predicate(code, lineno):
            return
        self.nhits.setdefault(label(code), Counter())[lineno] += 1
        if self.disable:
            return MON.DISABLE


if USE_SYS_MONITORING:
    MON = SysMonHelper()


# -------------------------------------------------------------------- #
#                           Helper functions                           #
# -------------------------------------------------------------------- #


def enable_line_events(code: Optional[CodeType] = None) -> None:
    if code is None:
        MON.set_events(MON.get_events() | MON.LINE)
    else:
        MON.set_local_events(code, MON.get_local_events(code) | MON.LINE)


def disable_line_events(code: Optional[CodeType] = None) -> None:
    if code is None:
        events = MON.get_events()
        if events & MON.LINE:
            MON.set_events(events ^ MON.LINE)
    else:
        events = MON.get_local_events(code)
        if events & MON.LINE:
            MON.set_local_events(code, events | MON.LINE)


# -------------------------------------------------------------------- #
#                                Tests                                 #
# -------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def sys_mon_cleanup() -> Generator[None, None, None]:
    """
    If :py:mod:`sys.monitoring` is available:
    * Remember all the relevant global callbacks before running the
      test,
    * If ``sys.monitoring.PROFILER_ID`` isn't already in use, mark it as
      being used by `'line_profiler_tests'`, and
    * Finally, restore these after the test:
      - The callbacks
      - The tool name (if we have set it earlier)
      - The globally-active events (or the lack thereof) for
        ``sys.monitoring.PROFILER_ID``

    otherwise, automatically :py:func:`pytest.skip` the test.
    """
    def restore(message):
        for name, callback in callbacks.items():
            prev_callback = MON.register_callback(event_ids[name], callback)
            if prev_callback is callback:
                callback_repr = '(UNCHANGED)'
            else:
                callback_repr = '-> ' + repr(callback)
            print('{} (`sys.monitoring.events.{}`): {!r} {}'.format(
                message, name, prev_callback, callback_repr))

    if not USE_SYS_MONITORING:
        pytest.skip('No `sys.monitoring`')
    # Remember the callbacks
    event_ids = {name: getattr(MON, name)
                 for name in ('LINE', 'PY_RETURN', 'PY_YIELD')}
    callbacks = {name: MON.register_callback(event_id, None)
                 for name, event_id in event_ids.items()}
    # Restore the callbacks since we "popped" them
    restore('Pre-test: putting the callbacks back')
    # Set the tool name if it isn't set already
    set_tool_id = not MON.get_tool()
    if set_tool_id:
        MON.use_tool_id('line_profiler_tests')
    try:
        with restore_events():
            yield
    finally:
        # Restore the callbacks
        restore('Post-test: restoring the callbacks')
        # Unset the temporary tool name
        if set_tool_id:
            MON.free_tool_id()


def test_standalone_callback_usage() -> None:
    """
    Check that :py:mod:`sys.monitoring` callbacks behave as expected
    when `LineProfiler`s are not in use.
    """
    _test_callback_helper(6, 7, 8, 9)


@pytest.mark.parametrize('wrap_trace', [True, False])
def test_wrapping_trace(wrap_trace: bool) -> None:
    """
    Check that existing :py:mod:`sys.monitoring` callbacks behave as
    expected depending on `LineProfiler.wrap_trace`.
    """
    prof = LineProfiler(wrap_trace=wrap_trace)
    try:
        nhits_expected = _test_callback_helper(
            6, 7, 8, 9, prof=prof, wrap=True, callback_called=wrap_trace)
    finally:
        with StringIO() as sio:
            prof.print_stats(sio)
            output = sio.getvalue()
        print(output)
    line = next(line for line in output.splitlines()
                if line.endswith('# Loop body'))
    nhits = int(line.split()[1])
    assert nhits == nhits_expected


def _test_callback_helper(
        nloop_no_trace: int,
        nloop_trace_global: int,
        nloop_trace_local: int,
        nloop_disabled: int,
        prof: Optional[LineProfiler] = None,
        wrap: bool = False,
        callback_called: bool = True) -> int:
    cumulative_nhits = 0

    def func(n: int) -> int:
        x = 0
        for n in range(1, n + 1):
            x += n  # Loop body
        return x

    def get_loop_hits() -> int:
        nonlocal cumulative_nhits
        cumulative_nhits = callback.nhits[label(code)][lineno_loop]
        return cumulative_nhits

    lines, first_lineno = inspect.getsourcelines(func)
    lineno_loop = first_lineno + next(
        offset for offset, line in enumerate(lines)
        if line.rstrip().endswith('# Loop body'))
    names = {func.__name__, func.__qualname__}
    code = func.__code__
    if prof is not None:
        if wrap:
            orig_func, func = func, prof(func)
            code = orig_func.__code__
        else:
            prof.add_callable(func)
            code = func.__code__
    callback = LineCallback(lambda code, _: code.co_name in names)

    # When line events are suppressed, nothing should happen
    with restore_events():
        disable_line_events()
        n = nloop_no_trace
        assert func(n) == n * (n + 1) // 2
        assert not callback.nhits

    # When line events are activated, the callback should see line
    # events
    with restore_events():  # Global events
        enable_line_events()
        n = nloop_trace_global
        assert func(n) == n * (n + 1) // 2
        print(callback.nhits)
        if callback_called:
            expected = cumulative_nhits + n
            assert get_loop_hits() == expected
        else:
            assert not callback.nhits
    with ExitStack() as stack:
        stack.enter_context(restore_events())
        stack.enter_context(restore_events(code=code))
        # Disable global line events, and enable local line events
        disable_line_events()
        enable_line_events(code)
        n = nloop_trace_local
        assert func(n) == n * (n + 1) // 2
        print(callback.nhits)
        if callback_called:
            expected = cumulative_nhits + n
            assert get_loop_hits() == expected
        else:
            assert not callback.nhits

    # Line events can be disabled on the specific line by returning
    # `sys.monitoring.DISABLE` from the callback
    for enable_global in True, False:
        callback.disable = True
        with ExitStack() as stack:
            stack.enter_context(restore_events())
            stack.enter_context(restore_events(code=code))
            # Set the global and local events
            # (doesn't matter if events are enabled globally or not)
            if enable_global:
                enable_line_events()
            else:
                disable_line_events()
            enable_line_events(code)
            # We still get 1 more hit because that's the call we
            # return `sys.monitoring.DISABLE` from
            n = nloop_disabled
            assert func(n) == n * (n + 1) // 2
            print(callback.nhits)
            if callback_called:
                expected = cumulative_nhits + 1
                assert get_loop_hits() == expected
            else:
                assert not callback.nhits
        MON.restart_events()

    # Return the total number of loops run
    # (Note: `nloop_disabled` is used twice)
    return (nloop_no_trace
            + nloop_trace_global
            + nloop_trace_local
            + 2 * nloop_disabled)
