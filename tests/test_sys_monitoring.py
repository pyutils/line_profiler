import inspect
import sys
from collections import Counter
from contextlib import AbstractContextManager, ExitStack
from functools import partial
from io import StringIO
from itertools import count
from types import CodeType, ModuleType
from typing import (Any, Optional, Union,
                    Callable, Generator,
                    Dict, FrozenSet, Tuple,
                    ClassVar)

import pytest

from line_profiler import _line_profiler, LineProfiler


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

    def get_current_callback(
            self, event_id: Optional[int] = None) -> Union[Callable, None]:
        """
        Arguments:
            event_id (int | None)
                Optional integer ID to retrieve the callback from;
                defaults to :py:data:`sys.monitoring.events.LINE`.

        Returns:
            The current callback (if any) associated with with
            :py:data:`~.tool_id` and ``event_id``.
        """
        register = self.register_callback
        if event_id is None:
            event_id = MON.LINE
        result = register(event_id, None)
        if result is not None:
            register(event_id, result)
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
            self.register()

    def register(self) -> Any:
        """
        Returns:
            Old value of the :py.mod:`sys.monitoring` callback.

        Side effects:
            Instance registered as the new :py:mod:`sys.monitoring`
            callback.
        """
        return MON.register_callback(MON.LINE, self)

    def handle_line_event(self, code: CodeType, lineno: int) -> bool:
        """
        Returns:
            Whether a line event has been recorded.

        Side effects:
            Entry created/incremented in :py:attr:`~.nhits` if
            :py:attr:`~.predicate` evaluates to true.
        """
        result = self.predicate(code, lineno)
        if result:
            self.nhits.setdefault(
                _line_profiler.label(code), Counter())[lineno] += 1
        return result

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
        if self.handle_line_event(code, lineno) and self.disable:
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
            MON.set_local_events(code, events ^ MON.LINE)


# -------------------------------------------------------------------- #
#                                Tests                                 #
# -------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def sys_mon_cleanup(
        monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """
    If :py:mod:`sys.monitoring` is available:
    * Make sure that we are using the default behavior by overriding
      :py:data:`line_profiler._line_profiler.USE_LEGACY_TRACE`;
    * Remember all the relevant global callbacks before running the
      test;
    * If ``sys.monitoring.PROFILER_ID`` isn't already in use, mark it as
      being used by `'line_profiler_tests'`; and
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
        monkeypatch.setattr(_line_profiler, 'USE_LEGACY_TRACE', False)
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
            6, 7, 8, 9, prof=prof, callback_called=wrap_trace)
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
        callback_called: bool = True) -> int:
    cumulative_nhits = 0

    def func(n: int) -> int:
        x = 0
        for n in range(1, n + 1):
            x += n  # Loop body
        return x

    def get_loop_hits() -> int:
        nonlocal cumulative_nhits
        cumulative_nhits = (
            callback.nhits[_line_profiler.label(code)][lineno_loop])
        return cumulative_nhits

    lines, first_lineno = inspect.getsourcelines(func)
    lineno_loop = first_lineno + next(
        offset for offset, line in enumerate(lines)
        if line.rstrip().endswith('# Loop body'))
    names = {func.__name__, func.__qualname__}
    code = func.__code__
    if prof is not None:
        orig_func, func = func, prof(func)
        code = orig_func.__code__
    callback = LineCallback(lambda code, _: code.co_name in names)

    # When line events are suppressed, nothing should happen
    with restore_events():
        disable_line_events()
        n = nloop_no_trace
        assert MON.get_current_callback() is callback
        assert func(n) == n * (n + 1) // 2
        assert MON.get_current_callback() is callback
        assert not callback.nhits

    # When line events are activated, the callback should see line
    # events
    with restore_events():  # Global events
        enable_line_events()
        n = nloop_trace_global
        assert MON.get_current_callback() is callback
        assert func(n) == n * (n + 1) // 2
        assert MON.get_current_callback() is callback
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
        assert MON.get_current_callback() is callback
        assert func(n) == n * (n + 1) // 2
        assert MON.get_current_callback() is callback
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
            assert MON.get_current_callback() is callback
            assert func(n) == n * (n + 1) // 2
            assert MON.get_current_callback() is callback
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


@pytest.mark.parametrize('standalone', [True, False])
def test_callback_switching(standalone: bool) -> None:
    """
    Check that hot-swapping of :py:mod:`sys.monitoring` callbacks
    behaves as expected, no matter if `LineProfiler`s are in use.
    """
    if standalone:
        prof: Union[LineProfiler, None] = None
    else:
        prof = LineProfiler(wrap_trace=True)

    try:
        nhits_expected = _test_callback_switching_helper(17, prof)
    finally:
        if prof is not None:
            with StringIO() as sio:
                prof.print_stats(sio)
                output = sio.getvalue()
            print(output)
    if prof is None:
        return

    line = next(line for line in output.splitlines()
                if line.endswith('# Loop body'))
    nhits = int(line.split()[1])
    assert nhits == nhits_expected


def _test_callback_switching_helper(
        nloop: int, prof: Optional[LineProfiler] = None) -> int:
    cumulative_nhits = 0, 0

    def func(n: int) -> int:
        x = 0
        for n in range(1, n + 1):
            x += n  # Loop body
        return x

    def get_loop_hits() -> Tuple[int, int]:
        nonlocal cumulative_nhits
        cumulative_nhits = tuple(  # type: ignore[assignment]
            callback.nhits.get(
                _line_profiler.label(code), Counter())[lineno_loop]
            for callback in (callback_1, callback_2))
        return cumulative_nhits

    def predicate(code: CodeType, lineno: int) -> bool:
        return code.co_name in names and lineno == lineno_loop

    class SwitchingCallback(LineCallback):
        """
        Callback which switches to the next one after having been
        triggered.
        """
        next: Union['SwitchingCallback', None]

        def __init__(self, *args,
                     next: Optional['SwitchingCallback'] = None,
                     **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.next = next

        def __call__(self, code: CodeType, lineno: int) -> Any:
            if not self.handle_line_event(code, lineno):
                return
            if self.next is not None:
                self.next.register()
            if self.disable:
                return MON.DISABLE

    lines, first_lineno = inspect.getsourcelines(func)
    lineno_loop = first_lineno + next(
        offset for offset, line in enumerate(lines)
        if line.rstrip().endswith('# Loop body'))
    names = {func.__name__, func.__qualname__}
    code = func.__code__
    if prof is not None:
        orig_func, func = func, prof(func)
        code = orig_func.__code__

    # The two callbacks hand off to one another in a loop
    callback_1 = SwitchingCallback(predicate)
    callback_2 = SwitchingCallback(predicate, register=False, next=callback_1)
    callback_1.next = callback_2

    with restore_events():
        enable_line_events()
        assert MON.get_current_callback() is callback_1
        assert func(nloop) == nloop * (nloop + 1) // 2
        assert MON.get_current_callback() in (callback_1, callback_2)
        print(callback_1.nhits, callback_2.nhits)
        nhits_one = nloop // 2
        nhits_other = nloop - nhits_one
        if nhits_one == nhits_other:
            assert get_loop_hits() == (nhits_one, nhits_other)
        else:  # Odd number
            assert get_loop_hits() in ((nhits_one, nhits_other),
                                       (nhits_other, nhits_one))

    return nloop


@pytest.mark.parametrize('add_events', [True, False])
@pytest.mark.parametrize('code_local_events', [True, False])
@pytest.mark.parametrize('start_with_events', [True, False])
@pytest.mark.parametrize('standalone', [True, False])
def test_callback_update_events(
        standalone: bool, start_with_events: bool,
        code_local_events: bool, add_events: bool) -> None:
    """
    Check that a :py:mod:`sys.monitoring` callback which updates the
    event set (global and code-object-local) after a certain number of
    hits behaves as expected, no matter if `LineProfiler`s are in use.
    """
    nloop = 10
    nloop_update = 5
    cumulative_nhits = 0

    def func(n: int) -> int:
        x = 0
        for n in range(1, n + 1):
            x += n  # Loop body
        return x

    def get_loop_hits() -> int:
        nonlocal cumulative_nhits
        cumulative_nhits = (
            callback.nhits[_line_profiler.label(code)][lineno_loop])
        return cumulative_nhits

    class EventUpdatingCallback(LineCallback):
        """
        Callback which, after a certain number of hits:
        - Disables :py:attr:`sys.monitoring.LINE` events, and
        - Enables :py:attr:`sys.monitoring.CALL` events (if
          :py:attr:`~.call` is true)
        """
        def __init__(self, *args,
                     code: Optional[CodeType] = None, call: bool = False,
                     **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.count = 0
            self.code = code
            self.call = call

        def __call__(self, code: CodeType, lineno: int) -> None:
            if not self.handle_line_event(code, lineno):
                return
            self.count += 1
            if self.count >= nloop_update:
                disable_line_events(self.code)
                if not self.call:
                    return
                if self.code is None:
                    MON.set_events(MON.get_events() | MON.CALL)
                else:
                    MON.set_local_events(
                        self.code, MON.get_local_events(self.code) | MON.CALL)

    lines, first_lineno = inspect.getsourcelines(func)
    lineno_loop = first_lineno + next(
        offset for offset, line in enumerate(lines)
        if line.rstrip().endswith('# Loop body'))
    names = {func.__name__, func.__qualname__}
    code = func.__code__

    if standalone:
        prof: Union[LineProfiler, None] = None
    else:
        prof = LineProfiler(wrap_trace=True)
    if start_with_events:
        global_events = MON.CALL
    else:
        global_events = MON.NO_EVENTS
    if prof is not None:
        orig_func, func = func, prof(func)
        code = orig_func.__code__

    callback = EventUpdatingCallback(
        lambda code, lineno: (code.co_name in names and lineno == lineno_loop),
        code=code if code_local_events else None,
        call=add_events)

    local_events = local_events_after = MON.NO_EVENTS
    global_events_after = global_events
    if code_local_events:
        local_events |= MON.LINE
        if add_events:
            local_events_after |= MON.CALL
    else:
        global_events |= MON.LINE
        if add_events:
            global_events_after |= MON.CALL
    MON.set_events(global_events)
    MON.set_local_events(code, local_events)

    try:
        # Check that data is only gathered for the first `nloop_update`
        # hits
        assert MON.get_current_callback() is callback
        assert func(nloop) == nloop * (nloop + 1) // 2
        assert MON.get_current_callback() is callback
        print(callback.nhits)
        assert get_loop_hits() == nloop_update
        # Check that the callback has disabled LINE events (and enabled
        # CALL events where appropriate)
        assert MON.get_events() == global_events_after
        assert MON.get_local_events(code) == local_events_after
    finally:
        if prof is not None:
            with StringIO() as sio:
                prof.print_stats(sio)
                output = sio.getvalue()
            print(output)
    if prof is None:
        return

    line = next(line for line in output.splitlines()
                if line.endswith('# Loop body'))
    nhits = int(line.split()[1])
    assert nhits == nloop


@pytest.mark.parametrize('standalone', [True, False])
def test_callback_toggle_local_events(standalone: bool) -> None:
    """
    Check that a :py:mod:`sys.monitoring` callback which disables local
    LINE events and later re-enables them with
    :py:func:`sys.monitoring.restart_events` behaves as expected, no
    matter if `LineProfiler`s are in use.
    """
    if standalone:
        prof: Union[LineProfiler, None] = None
    else:
        prof = LineProfiler(wrap_trace=True)

    try:
        nhits_expected = _test_callback_toggle_local_events_helper(
            17, 18, 19, prof)
    finally:
        if prof is not None:
            with StringIO() as sio:
                prof.print_stats(sio)
                output = sio.getvalue()
            print(output)
    if prof is None:
        return

    line = next(line for line in output.splitlines()
                if line.endswith('# Loop body'))
    nhits = int(line.split()[1])
    assert nhits == nhits_expected


def _test_callback_toggle_local_events_helper(
        nloop_before_disabling: int,
        nloop_when_disabled: int,
        nloop_after_reenabling: int,
        prof: Optional[LineProfiler] = None) -> int:
    cumulative_nhits = 0

    def func(*nloops) -> int:
        x = 0
        counter = count(1)
        for n in nloops:
            for _ in range(n):
                x += next(counter)  # Loop body
            pass  # Switching location
        return x

    def get_loop_hits() -> int:
        nonlocal cumulative_nhits
        cumulative_nhits = (
            callback.nhits[_line_profiler.label(code)][lineno_loop])
        return cumulative_nhits

    class LocalDisablingCallback(LineCallback):
        """
        Callback which disables LINE events locally after a certain
        number of hits
        """
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.switch_count = 0

        def __call__(self, code: CodeType, lineno: int) -> Any:
            if not self.handle_line_event(code, lineno):
                return
            # When we hit the "loop" line after having hit the "switch"
            # line, disable line events on the "loop" line
            if lineno == lineno_loop and self.switch_count % 2:
                # Remove the recorded hit
                self.nhits[_line_profiler.label(code)][lineno] -= 1
                return MON.DISABLE
            # When we hit the "switch" line the second line, restart
            # events to undo disabling of the "loop" line
            if lineno == lineno_switch:
                if self.switch_count % 2:
                    MON.restart_events()
                self.switch_count += 1
                return

    lines, first_lineno = inspect.getsourcelines(func)
    lineno_loop = first_lineno + next(
        offset for offset, line in enumerate(lines)
        if line.rstrip().endswith('# Loop body'))
    lineno_switch = first_lineno + next(
        offset for offset, line in enumerate(lines)
        if line.rstrip().endswith('# Switching location'))
    linenos = {lineno_loop, lineno_switch}
    names = {func.__name__, func.__qualname__}
    code = func.__code__
    if prof is not None:
        orig_func, func = func, prof(func)
        code = orig_func.__code__

    callback = LocalDisablingCallback(
        lambda code, lineno: (code.co_name in names and lineno in linenos))

    MON.set_events(MON.get_events() | MON.LINE)
    n = nloop_before_disabling + nloop_when_disabled + nloop_after_reenabling
    assert MON.get_current_callback() is callback
    assert func(nloop_before_disabling,
                nloop_when_disabled,
                nloop_after_reenabling) == n * (n + 1) // 2
    assert MON.get_current_callback() is callback
    print(callback.nhits)
    assert get_loop_hits() == nloop_before_disabling + nloop_after_reenabling

    return n
