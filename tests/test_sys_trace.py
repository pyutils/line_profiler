"""
Test the interoperability between `LineProfiler` and other `sys` tracing
facilities (e.g. Python functions registered via `sys.settrace()`.

Notes
-----
- By the very nature of the tests in this test module, they override
  `sys` trace functions, and are thus largely opaque towards
  `coverage.py`.
- However, there effects are isolated since each test is run in a
  separate Python subprocess.
"""
import concurrent.futures
import functools
import inspect
import linecache
import os
import subprocess
import shlex
import sys
import time
import tempfile
import textwrap
import threading
import pytest
from ast import literal_eval
from contextlib import nullcontext
from io import StringIO
from types import FrameType, ModuleType
from typing import Any, Optional, Union, Callable, List, Literal
from line_profiler import LineProfiler


# Common utilities

DEBUG = False
USE_SYS_MONITORING = isinstance(getattr(sys, 'monitoring', None), ModuleType)

Event = Literal['call', 'line', 'return', 'exception', 'opcode']
TracingFunc = Callable[[FrameType, Event, Any], Union['TracingFunc', None]]


def strip(s: str) -> str:
    return textwrap.dedent(s).strip('\n')


def isolate_test_in_subproc(
        func: Optional[Callable] = None, debug: bool = DEBUG) -> Callable:
    """
    Run the test function with the supplied arguments in a subprocess so
    that it doesn't pollute the state of the current interpretor.
    If `debug` is true, run with `pytest` for more detailed traceback.

    Notes
    -----
    - Code is written to a tempfile and run in a subprocess.
    - The test function should be import-able from the top-level
      namespace of this file.
    - All the arguments should be `ast.literal_eval()`-able.
    - Beware of using fixtures for these tests.
    """
    if func is None:
        return functools.partial(isolate_test_in_subproc, debug=debug)

    def message(msg: str, header: str, *,
                short: bool = False, **kwargs) -> None:
        header = strip(header)
        if not header.endswith(':'):
            header += ':'
        kwargs['sep'] = '\n'
        if short and len(msg.splitlines()) < 2:
            print('', f'{header} {msg}', **kwargs)
            return
        print('', header, textwrap.indent(msg, '  '), **kwargs)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Check if the function is importable
        test_func = func.__name__
        assert globals()[test_func].__subproc_test_inner__ is func

        # Check if the arguments are round-trippable
        assert literal_eval(repr(args)) == args
        assert literal_eval(repr(kwargs)) == kwargs

        # Write a test script
        args_reprs = ([repr(a) for a in args]
                      + [f'{k}={v!r}' for k, v in kwargs.items()])
        if len(args_reprs) > 1:
            args_repr = '\n' + textwrap.indent(',\n'.join(args_reprs), ' ' * 8)
        else:
            args_repr = ', '.join(args_reprs)
        code_template = strip("""
        import sys
        sys.path.insert(0,  # Let the test import from this file
                        {path!r})
        from {mod} import (  # Import the test func from this file
            {test} as _{test})

        def {test}():
            _{test}.__subproc_test_inner__({args})

        if __name__ == '__main__':
            {test}()
        """)
        test_dir, test_filename = os.path.split(__file__)
        test_module_name, dot_py = os.path.splitext(test_filename)
        assert dot_py == '.py'
        code = code_template.format(path=test_dir, mod=test_module_name,
                                    test=test_func, args=args_repr)
        # Run the test script in a subprocess
        if debug:  # Use `pytest` to get perks like assertion rewriting
            cmd = [sys.executable, '-m', 'pytest', '-s']
        else:
            cmd = [sys.executable]
        with tempfile.TemporaryDirectory() as tmpdir:
            curdir = os.path.abspath(os.curdir)
            os.chdir(tmpdir)
            fname = 'my_test.py'
            cmd.append(fname)
            message(code, f'Test code run ({shlex.quote(fname)})')
            message(shlex.join(cmd), 'Executed command')
            try:
                with open(fname, mode='w') as fobj:
                    print(code, file=fobj)
                proc = subprocess.run(cmd, capture_output=True, text=True)
            finally:
                os.chdir(curdir)
        if proc.stdout:
            message(proc.stdout, 'Stdout')
        else:
            message('<N/A>', 'Stdout', short=True)
        if proc.stderr:
            message(proc.stderr, 'Stderr', file=sys.stderr)
        else:
            message('<N/A>', 'Stderr', short=True)
        proc.check_returncode()

    wrapper.__subproc_test_inner__ = func
    return wrapper


def foo(n: int) -> int:
    result = 0
    for spam in range(1, n + 1):
        result += spam
    return result


def bar(n: int) -> int:
    result = 0
    for ham in range(1, n + 1):
        result += ham
    return result


def baz(n: int) -> int:
    result = 0
    for eggs in range(1, n + 1):
        result += eggs
    return result


class suspend_tracing:
    def __init__(self):
        self.callback = None
        self.events = 0

    def __enter__(self):
        if USE_SYS_MONITORING:
            mod = sys.monitoring
            self.events = mod.get_events(mod.PROFILER_ID)
            mod.set_events(mod.PROFILER_ID, mod.events.NO_EVENTS)
        else:
            self.callback = sys.gettrace()
            sys.settrace(None)

    def __exit__(self, *_, **__):
        if USE_SYS_MONITORING:
            mod = sys.monitoring
            mod.set_events(mod.PROFILER_ID, self.events)
            self.events = 0
        else:
            sys.settrace(self.callback)
            self.callback = None


def get_incr_logger(logs: List[str], func: Literal[foo, bar, baz] = foo, *,
                    bugged: bool = False,
                    report_return: bool = False) -> TracingFunc:
    '''
    Append a '<func>: spam = <...>' message whenever we hit the line in
    `func()` containing the incrementation of `result`.
    If it's made `bugged`, it sets the frame's `.f_trace_lines` to false
    after writing the first log entry, disabling line events.
    If `report_return` is true, a 'Returning from <func>()' log entry
    is written on return.
    '''
    def callback(
            frame: FrameType, event: Event, _) -> Union[TracingFunc, None]:
        if DEBUG and callback.emit_debug:
            print('{0.co_filename}:{1.f_lineno} - {0.co_name} ({2})'
                  .format(frame.f_code, frame, event))
        if event == 'call':  # Set up tracing for nested scopes
            return callback
        if event not in events:  # Only trace the specified events
            return
        code = frame.f_code
        if code.co_filename != filename or code.co_name != func_name:
            return
        if event == 'return':  # Write a return entry where appropriate
            logs.append(f'Returning from `{func_name}()`')
            return
        if frame.f_lineno == lineno:
            # Add log entry whenever the target line is hit
            counter_value = frame.f_locals.get(counter)
            logs.append(f'{func_name}: {counter} = {counter_value}')
            if bugged:  # Line-event tracing turned off after first hit
                frame.f_trace_lines = False
        return callback

    # Get data from `func()`: its (file-)name, the line number of the
    # incrementation, and the name of the counter variable
    func_name = func.__name__
    filename = func.__code__.co_filename
    lineno = func.__code__.co_firstlineno
    block = inspect.getblock(linecache.getlines(__file__)[lineno - 1:])
    (offset, line), = ((i, line) for i, line in enumerate(block)
                       if 'result +=' in line)
    lineno += offset
    counter = line.split()[-1]

    events = {'line'}
    if report_return:
        events.add('return')

    callback.emit_debug = False
    return callback


def get_return_logger(logs: List[str], *, bugged: bool = False) -> TracingFunc:
    '''
    Append a 'Returning from `<func>()`' message whenever we hit return
    from a function defined in this file. If it's made `bugged`, it
    panics and errors out when returning from `bar`, thus unsetting the
    `sys` trace.
    '''
    def callback(
            frame: FrameType, event: Event, _) -> Union[TracingFunc, None]:
        if DEBUG and callback.emit_debug:
            print('{0.co_filename}:{1.f_lineno} - {0.co_name} ({2})'
                  .format(frame.f_code, frame, event))
        if event == 'call':
            # Set up tracing for nested scopes
            return callback
        if event != 'return':
            return  # Only trace return events
        code = frame.f_code
        if code.co_filename != __file__:
            return  # Only trace functions in this file
        # Add log entry
        logs.append(f'Returning from `{code.co_name}()`')
        if bugged and code.co_name == 'bar':
            # Error out and cause `sys.settrace(None)`
            raise MyException

    callback.emit_debug = False
    return callback


class MyException(Exception):
    """Unique exception raised by some of the tests."""
    pass


# Tests


def _test_helper_callback_preservation(
        callback: Union[TracingFunc, None]) -> None:
    sys.settrace(callback)
    assert sys.gettrace() is callback, f'can\'t set trace to {callback!r}'
    profile = LineProfiler(wrap_trace=False)
    profile.enable_by_count()
    if not USE_SYS_MONITORING:
        assert profile in sys.gettrace().active_instances, (
            'can\'t set trace to the profiler')
    profile.disable_by_count()
    assert sys.gettrace() is callback, f'trace not restored to {callback!r}'
    sys.settrace(None)


@isolate_test_in_subproc
def test_callback_preservation():
    """
    Test in a subprocess that the profiler restores the active `sys`
    trace callback (or the lack thereof) after it's `.disable()`-ed.
    """
    _test_helper_callback_preservation(None)
    _test_helper_callback_preservation(lambda frame, event, arg: None)


@pytest.mark.parametrize(
    ('label', 'use_profiler', 'wrap_trace'),
    [('base case', False, False),
     ('profiled (trace suspended)', True, False),
     ('profiled (trace wrapped)', True, True)])
@isolate_test_in_subproc
def test_callback_wrapping(
        label: str, use_profiler: bool, wrap_trace: bool) -> None:
    """
    Test in a subprocess that the profiler can wrap around an existing
    trace callback such that we both profile the code and do whatever
    the existing callback does.
    """
    logs = []
    my_callback = get_incr_logger(logs)
    sys.settrace(my_callback)

    if use_profiler:
        profile = LineProfiler(wrap_trace=wrap_trace)
        foo_like = profile(foo)
        trace_preserved = wrap_trace
    else:
        foo_like = foo
        trace_preserved = True
    if trace_preserved or USE_SYS_MONITORING:
        exp_logs = [f'foo: spam = {spam}' for spam in range(1, 6)]
    else:
        exp_logs = []

    assert sys.gettrace() is my_callback, 'can\'t set custom trace'
    my_callback.emit_debug = True
    x = foo_like(5)
    my_callback.emit_debug = False
    assert x == 15, f'expected `foo(5) = 15`, got {x!r}'
    assert sys.gettrace() is my_callback, 'trace not restored afterwards'

    # Check that the existing trace function has been called where
    # appropriate
    print(f'Logs: {logs!r}')
    assert logs == exp_logs, f'expected logs = {exp_logs!r}, got {logs!r}'

    # Check that the profiling is as expected: 5 hits on the
    # incrementation line
    if not use_profiler:
        return
    with StringIO() as sio:
        profile.print_stats(stream=sio, summarize=True)
        out = sio.getvalue()
    print(out)
    line, = (line for line in out.splitlines() if '+=' in line)
    nhits = int(line.split()[1])
    assert nhits == 5, f'expected 5 profiler hits, got {nhits!r}'


@pytest.mark.parametrize(
    ('label', 'use_profiler', 'enable_count'),
    [('base case', False, 0),
     ('profiled (isolated)', True, 0),
     ('profiled (continuous)', True, 1)])
@isolate_test_in_subproc
def test_wrapping_throwing_callback(
        label: str, use_profiler: bool, enable_count: int) -> None:
    """
    Test in a subprocess that if the profiler wraps around an existing
    trace callback that errors out:
    - Profiling continues uninterrupted.
    - The errored-out trace callback is no longer called from the
      profiling traceback.
    - The `sys` traceback is set to `None` when the profiler is
      `.disable()`-ed.

    Notes
    -----
    Extra `enable_count` means that the profiler stays enabled between
    the calls to the profiled functions, and we thereyby test against
    these problematic behaviors after `my_callback()` bugs out:
    - If the profiler stops profiling (because the `sys` trace callback
      is unset), or
    - If the profiler's callback keeps calling `my_callback()`
      afterwards.
    """
    logs = []
    my_callback = get_return_logger(logs, bugged=True)
    sys.settrace(my_callback)
    assert sys.gettrace() is my_callback, 'can\'t set custom trace'

    if use_profiler:
        profile = LineProfiler(wrap_trace=True)
        foo_like, bar_like, baz_like = profile(foo), profile(bar), profile(baz)
    else:
        foo_like, bar_like, baz_like = foo, bar, baz
        enable_count = 0

    for _ in range(enable_count):
        profile.enable_by_count()
    my_callback.emit_debug = True
    x = foo_like(3)  # This is logged
    try:
        _ = bar_like(4)  # This is also logged, but...
    except MyException:
        # ... the trace func errors out as `bar()` returns, and as such
        # disables itself
        pass
    else:
        assert False, 'tracing function didn\'t error out'
    y = baz_like(5)  # Not logged because trace disabled itself
    my_callback.emit_debug = False
    for _ in range(enable_count):
        profile.disable_by_count()

    assert x == 6, f'expected `foo(3) = 6`, got {x!r}'
    assert y == 15, f'expected `baz(5) = 15`, got {y!r}'
    assert sys.gettrace() is None, (
        '`sys` trace = {sys.gettrace()!r} not reset afterwards')

    # Check that the existing trace function has been called where
    # appropriate
    print(f'Logs: {logs!r}')
    exp_logs = ['Returning from `foo()`', 'Returning from `bar()`']
    assert logs == exp_logs, f'expected logs = {exp_logs!r}, got {logs!r}'

    # Check that the profiling is as expected: 3 (resp. 4, 5) hits on
    # the incrementation line for `foo()` (resp. `bar()`, `baz()`)
    if not use_profiler:
        return
    with StringIO() as sio:
        profile.print_stats(stream=sio, summarize=True)
        out = sio.getvalue()
    print(out)
    for func, marker, exp_nhits in [('foo', 'spam', 3), ('bar', 'ham', 4),
                                    ('baz', 'eggs', 5)]:
        line, = (line for line in out.splitlines()
                 if line.endswith('+= ' + marker))
        nhits = int(line.split()[1])
        assert nhits == exp_nhits, (f'expected {exp_nhits} '
                                    f'profiler hits, got {nhits!r}')


@pytest.mark.parametrize(('label', 'use_profiler'),
                         [('base case', False), ('profiled', True)])
@isolate_test_in_subproc
def test_wrapping_line_event_disabling_callback(label: str,
                                                use_profiler: bool) -> None:
    """
    Test in a subprocess that if the profiler wraps around an existing
    trace callback that disables `.f_trace_lines`:
    - Profiling continues uninterrupted.
    - `.f_trace` is subsequently disabled, but only for line events in
      that frame.
    """
    logs = []
    my_callback = get_incr_logger(logs, bugged=True, report_return=True)
    sys.settrace(my_callback)

    if use_profiler:
        profile = LineProfiler(wrap_trace=True)
        foo_like = profile(foo)
    else:
        foo_like = foo

    assert sys.gettrace() is my_callback, 'can\'t set custom trace'
    my_callback.emit_debug = True
    x = foo_like(5)
    my_callback.emit_debug = False
    assert x == 15, f'expected `foo(5) = 15`, got {x!r}'
    assert sys.gettrace() is my_callback, 'trace not restored afterwards'

    # Check that the trace function has been called exactly once on the
    # line event, and once on the return event
    print(f'Logs: {logs!r}')
    exp_logs = ['foo: spam = 1', 'Returning from `foo()`']
    assert logs == exp_logs, f'expected logs = {exp_logs!r}, got {logs!r}'

    # Check that the profiling is as expected: 5 hits on the
    # incrementation line
    if not use_profiler:
        return
    with StringIO() as sio:
        profile.print_stats(stream=sio, summarize=True)
        out = sio.getvalue()
    print(out)
    line, = (line for line in out.splitlines() if '+=' in line)
    nhits = int(line.split()[1])
    assert nhits == 5, f'expected 5 profiler hits, got {nhits!r}'


def _test_helper_wrapping_thread_local_callbacks(
        profile: Union[LineProfiler, None], sleep: float = .0625) -> str:
    logs = []
    if threading.current_thread() == threading.main_thread():
        thread_label = 'main'
        func = foo
        my_callback = get_incr_logger(logs, func)
        exp_logs = [f'foo: spam = {spam}' for spam in range(1, 6)]
    else:
        thread_label = 'side'
        func = bar
        my_callback = get_return_logger(logs)
        exp_logs = ['Returning from `bar()`']
    if profile is None:
        func_like = func
    else:
        func_like = profile(func)
    print(f'Thread: {threading.get_ident()} ({thread_label})')

    # Check result
    sys.settrace(my_callback)
    assert sys.gettrace() is my_callback, 'can\'t set custom trace'
    my_callback.emit_debug = True
    x = func_like(5)
    my_callback.emit_debug = False
    assert x == 15, f'expected `{func.__name__}(5) = 15`, got {x!r}'
    assert sys.gettrace() is my_callback, 'trace not restored afterwards'

    # Check logs
    print(f'Logs ({thread_label} thread): {logs!r}')
    assert logs == exp_logs, f'expected logs = {exp_logs!r}, got {logs!r}'
    time.sleep(sleep)
    return '\n'.join(logs)


@pytest.mark.parametrize(('label', 'use_profiler'),
                         [('base case', False), ('profiled', True)])
@isolate_test_in_subproc
def test_wrapping_thread_local_callbacks(label: str,
                                         use_profiler: bool) -> None:
    """
    Test in a subprocess that the profiler properly handles thread-local
    `sys` trace callbacks.
    """
    profile = LineProfiler(wrap_trace=True) if use_profiler else None
    expected_results = {
        # From the main thread
        '\n'.join(f'foo: spam = {spam}' for spam in range(1, 6)),
        # From the other thread
        'Returning from `bar()`',
    }

    # Run tasks (and so some basic checks)
    results = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        tasks = []
        tasks.append(executor.submit(  # This is run on a side thread
            _test_helper_wrapping_thread_local_callbacks, profile))
        # This is run on the main thread
        results.add(_test_helper_wrapping_thread_local_callbacks(profile))
        results.update(
            future.result()
            for future in concurrent.futures.as_completed(tasks))
    assert results == expected_results, (f'expected {expected_results!r}, '
                                         f'got {results!r}')

    # Check profiling
    if profile is None:
        return
    with StringIO() as sio:
        profile.print_stats(stream=sio, summarize=True)
        out = sio.getvalue()
    print(out)
    for var in 'spam', 'ham':
        line, = (line for line in out.splitlines()
                 if line.endswith('+= ' + var))
        nhits = int(line.split()[1])
        assert nhits == 5, f'expected 5 profiler hits, got {nhits!r}'


@pytest.mark.parametrize(
    ('stay_in_scope', 'set_frame_local_trace', 'n', 'nhits'),
    [(True, True, 100, {0: 2,  # Both calls are traced
                        5: 0,  # Tracing suspended
                        7: 100}),  # Tracing restored (both calls)
     # If `set_frame_local_trace` is false:
     # - When using legacy tracing, tracing is suspended for the rest of
     #   the frame
     # - Else, tracing is unaffected
     (True, False, 100,
      {0: 2, 5: 0, 7: 100 if USE_SYS_MONITORING else 0}),
     # Calling a function always triggers `<trace>.__call__()`
     (False, True, 100, {0: 1,  # Only one of the calls is traced
                         2: 100}),  # 100 hits on the line in the loop
     (False, False, 100, {0: 1, 2: 100})])
@isolate_test_in_subproc
def test_python_level_trace_manipulation(
        stay_in_scope, set_frame_local_trace, n, nhits):
    """
    Test that:
    - When Python code retrieves the trace object set by `line_profiler`
      with `sys.gettrace()` and later restores it via `sys.settrace()`,
      it doesn't break anything, and
    - Resumption of line profiling in the same frame thereafter happens
      if and only if `set_frame_local_trace` is true.
    """
    prof = LineProfiler(set_frame_local_trace=set_frame_local_trace)

    def func_no_break(n):
        x = 0
        for n in range(1, n + 1):
            x += n
        return x

    @prof
    def func_break_in_middle(n):
        x = 0
        n_not_traced = n // 2
        n_traced = n - n_not_traced
        with suspend_tracing():
            for n in range(1, n_not_traced + 1):
                x += n
        for n in range(n_not_traced + 1, n_not_traced + n_traced + 1):
            x += n
        return x

    prof.add_callable(func_no_break)
    expected = n * (n + 1) // 2

    if stay_in_scope:
        # Do two calls, each with tracing suspended for half of the loop
        outer_ctx = inner_ctx = nullcontext()
        func = func_break_in_middle
    else:
        # Do one call with tracing suspended, then another with it
        # restored
        outer_ctx = prof
        inner_ctx = suspend_tracing()
        func = func_no_break
    with outer_ctx:
        with inner_ctx:
            assert func(n) == expected
        assert func(n) == expected
    timings = prof.get_stats().timings
    print(timings)
    prof.print_stats()
    entries = next(entries for (*_, func_name), entries in timings.items()
                   if func_name.endswith(func.__name__))
    body_start_line = min(lineno for (lineno, *_) in entries)
    all_nhits = {lineno - body_start_line: _nhits
                 for (lineno, _nhits, _) in entries}
    all_nhits = {lineno: all_nhits.get(lineno, 0) for lineno in nhits}
    assert all_nhits == nhits, f'expected {nhits=}, got {all_nhits=}'
