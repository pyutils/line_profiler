# cython: language_level=3
# cython: infer_types=True
# cython: legacy_implicit_noexcept=True
# distutils: language=c++
# distutils: include_dirs = python25.pxd
r"""
This is the Cython backend used in :py:mod:`line_profiler.line_profiler`.

Ignore:
    # Standalone compile instructions for developers
    # Assuming the cwd is the repo root.
    cythonize --annotate --inplace \
        ./line_profiler/_line_profiler.pyx \
        ./line_profiler/timers.c
"""
from collections.abc import Callable
from functools import wraps
from sys import byteorder
import sys
cimport cython
from cython.operator cimport dereference as deref
from cpython.object cimport PyObject_Hash
from cpython.bytes cimport PyBytes_AS_STRING, PyBytes_GET_SIZE
from cpython.version cimport PY_VERSION_HEX
from libc.stdint cimport int64_t

from libcpp.unordered_map cimport unordered_map
import functools
import threading
import opcode
import os
import types
from warnings import warn
from weakref import WeakSet

from line_profiler._diagnostics import (
    WRAP_TRACE, SET_FRAME_LOCAL_TRACE, USE_LEGACY_TRACE
)

from ._map_helpers cimport (
    last_erase_if_present, line_ensure_entry, LastTime, LastTimeMap,
    LineTime, LineTimeMap
)


NOP_VALUE: int = opcode.opmap['NOP']

# The Op code should be 2 bytes as stated in
# https://docs.python.org/3/library/dis.html
# if sys.version_info[0:2] >= (3, 11):
NOP_BYTES_LEN: int = 2
NOP_BYTES: bytes = NOP_VALUE.to_bytes(NOP_BYTES_LEN, byteorder=byteorder)

# This should be true for Python >=3.11a1
HAS_CO_QUALNAME: bool = hasattr(types.CodeType, 'co_qualname')

# Can't line-profile Cython in 3.12 since the old C API was upended
# without an appropriate replacement (which only came in 3.13);
# see also:
# https://cython.readthedocs.io/en/latest/src/tutorial/profiling_tutorial.html
_CAN_USE_SYS_MONITORING = PY_VERSION_HEX >= 0x030c00b1
CANNOT_LINE_TRACE_CYTHON = (
    _CAN_USE_SYS_MONITORING and PY_VERSION_HEX < 0x030d00b1)

if not (USE_LEGACY_TRACE or _CAN_USE_SYS_MONITORING):
    # Shouldn't happen since we're already checking the existence of
    # `sys.monitoring` in `line_profiler._diagnostics`, but just to be
    # absolutely sure...
    warn("`sys.monitoring`-based line profiling selected but unavailable "
         f"in Python {sys.version}; falling back to the legacy trace system")
    USE_LEGACY_TRACE = True

# long long int is at least 64 bytes assuming c99
ctypedef unsigned long long int uint64
ctypedef long long int int64

cdef extern from "Python_wrapper.h":
    ctypedef struct PyObject
    ctypedef struct PyCodeObject
    ctypedef struct PyFrameObject
    ctypedef Py_ssize_t Py_hash_t
    ctypedef long long PY_LONG_LONG
    ctypedef int (*Py_tracefunc)(
        object self, PyFrameObject *py_frame, int what, PyObject *arg)

    cdef PyCodeObject* PyFrame_GetCode(PyFrameObject* frame)
    cdef int PyCode_Addr2Line(PyCodeObject *co, int byte_offset)

    cdef void PyEval_SetTrace(Py_tracefunc func, object arg)
    cdef PyObject* PyObject_Call(
        PyObject *callable, PyObject *args, PyObject *kwargs) except *

    # They're actually #defines, but whatever.
    cdef int PyTrace_CALL
    cdef int PyTrace_EXCEPTION
    cdef int PyTrace_LINE
    cdef int PyTrace_RETURN
    cdef int PyTrace_OPCODE
    cdef int PyTrace_C_CALL
    cdef int PyTrace_C_EXCEPTION
    cdef int PyTrace_C_RETURN

    cdef int PyFrame_GetLineNumber(PyFrameObject *frame)
    cdef void Py_XDECREF(PyObject *o)

    cdef unsigned long PyThread_get_thread_ident()

ctypedef PyCodeObject *PyCodeObjectPtr
#ctypedef unordered_map[int64, LastTime] LastTimeMap
#ctypedef unordered_map[int64, LineTime] LineTimeMap

cdef extern from "c_trace_callbacks.h":  # Legacy tracing
    ctypedef unsigned long long Py_uintptr_t

    ctypedef struct TraceCallback:
        Py_tracefunc c_tracefunc
        PyObject *c_traceobj

    cdef TraceCallback *alloc_callback() except *
    cdef void free_callback(TraceCallback *callback)
    cdef void populate_callback(TraceCallback *callback)
    cdef void restore_callback(TraceCallback *callback)
    cdef int call_callback(
        PyObject *disabler, TraceCallback *callback,
        PyFrameObject *py_frame, int what, PyObject *arg)
    cdef void set_local_trace(PyObject *manager, PyFrameObject *py_frame)
    cdef Py_uintptr_t monitoring_restart_version()

cdef extern from "timers.c":
    PY_LONG_LONG hpTimer()
    double hpTimerUnit()

#cdef struct LineTime:
#    int64 code
#    int lineno
#    PY_LONG_LONG total_time
#    long nhits

#cdef struct LastTime:
#    int f_lineno
#    PY_LONG_LONG time


cdef inline int64 compute_line_hash(uint64 block_hash, uint64 linenum) noexcept:
    """
    Compute the hash used to store each line timing in an unordered_map.
    This is fairly simple, and could use some improvement since linenum
    isn't technically random, however it seems to be good enough and
    fast enough for any practical purposes.
    """
    # linenum doesn't need to be int64 but it's really a temporary value
    # so it doesn't matter
    return block_hash ^ linenum


cdef inline object multibyte_rstrip(bytes bytecode):
    """
    Returns:
        result (tuple[bytes, int])
        - First item is the bare unpadded bytecode
        - Second item is the number of :py:data:`NOP_BYTES`
          ``bytecode`` has been padded with
    """
    npad: int = 0
    nop_len: int = -NOP_BYTES_LEN
    nop_bytes: bytes = NOP_BYTES
    unpadded: bytes = bytecode
    while unpadded.endswith(nop_bytes):
        unpadded = unpadded[:nop_len]
        npad += 1
    return (unpadded, npad)


cdef inline object get_current_callback(int tool_id, int event_id):
    """
    Note:
        Unfortunately there's no public API for directly retrieving
        the current callback, no matter on the Python side or the C
        side.  This may become a performance bottleneck...
    """
    mon = sys.monitoring
    cdef object register = mon.register_callback
    cdef object result = register(tool_id, event_id, None)
    if result is not None:
        register(tool_id, event_id, result)
    return result


def label(code):
    """
    Return a ``(filename, first_lineno, _name)`` tuple for a given code
    object.

    This is the similar labelling as used by the :py:mod:`cProfile`
    module in Python 2.5.

    Note:
        In Python >= 3.11 we return qualname for ``_name``.
        In older versions of Python we just return name.
    """
    if isinstance(code, str):
        return ('~', 0, code)  # built-in functions ('~' sorts at the end)
    else:
        if HAS_CO_QUALNAME:
            return (code.co_filename, code.co_firstlineno, code.co_qualname)
        else:
            return (code.co_filename, code.co_firstlineno, code.co_name)


def find_cython_source_file(cython_func):
    """
    Resolve the absolute path to a Cython function's source file.

    Returns:
        result (str | None)
            Cython source file if found, else :py:data:`None`.
    """
    try:
        compiled_module = cython_func.__globals__['__file__']
    except KeyError:  # Shouldn't happen...
        return None
    rel_source_file = cython_func.__code__.co_filename
    if os.path.isabs(rel_source_file):
        if os.path.isfile(rel_source_file):
            return rel_source_file
        return None
    prefix = os.path.dirname(compiled_module)
    while True:
        source_file = os.path.join(prefix, rel_source_file)
        if os.path.isfile(source_file):
            return source_file
        next_prefix = os.path.dirname(prefix)
        if next_prefix == prefix:  # At the file-system root
            return None
        prefix = next_prefix


def disable_line_events(trace_func: Callable) -> Callable:
    """
    Returns:
        trace_func (Callable)
            If it is a wrapper created by
            :py:attr:`_LineProfilerManager.wrap_local_f_trace`;
            ``trace_func.disable_line_events`` is also set to true
        wrapper (Callable)
            Otherwise, a thin wrapper around ``trace_func()`` which
            withholds line events.

    Note:
        This is for when a frame-local :py:attr:`~frame.f_trace`
        disables :py:attr:`~frame.f_trace_lines` -- we would like to
        keep line events enabled (so that line profiling works) while
        "unsubscribing" the trace function from it.
    """
    @wraps(trace_func)
    def wrapper(frame, event, arg):
        if event == 'line':
            return
        return trace_func(frame, event, arg)

    try:  # Disable the wrapper directly
        if hasattr(trace_func, '__line_profiler_manager__'):
            trace_func.disable_line_events = True
            return trace_func
    except AttributeError:
        pass
    return wrapper


cpdef _code_replace(func, co_code):
    """
    Implements :py:mod:`~code.replace` for Python < 3.8
    """
    try:
        code = func.__code__
    except AttributeError:
        code = func.__func__.__code__
    if hasattr(code, 'replace'):
        # python 3.8+
        code = _copy_local_sysmon_events(code, code.replace(co_code=co_code))
    else:
        # python <3.8
        co = code
        code = type(code)(co.co_argcount, co.co_kwonlyargcount,
                        co.co_nlocals, co.co_stacksize, co.co_flags,
                        co_code, co.co_consts, co.co_names,
                        co.co_varnames, co.co_filename, co.co_name,
                        co.co_firstlineno, co.co_lnotab, co.co_freevars,
                        co.co_cellvars)
    return code


cpdef _copy_local_sysmon_events(old_code, new_code):
    """
    Copy the local events from ``old_code`` over to ``new_code`` where
    appropriate.

    Returns:
        code: ``new_code``
    """
    try:
        mon = sys.monitoring
    except AttributeError:  # Python < 3.12
        return new_code
    # Tool ids are integers in the range 0 to 5 inclusive.
    # https://docs.python.org/3/library/sys.monitoring.html#tool-identifiers
    NUM_TOOLS = 6  
    for tool_id in range(NUM_TOOLS):
        try:
            events = mon.get_local_events(tool_id, old_code)
            mon.set_local_events(tool_id, new_code, events)
        except ValueError:  # Tool ID not in use
            pass
    return new_code


cpdef int _patch_events(int events, int before, int after) noexcept:
    """
    Patch ``events`` based on the differences between ``before`` and
    ``after``.

    Example:
        >>> events = 0b110000
        >>> before = 0b101101
        >>> after = 0b_001011  # Additions: 0b10, deletions: 0b100100
        >>> assert _patch_events(events, before, after) == 0b010010
    """
    cdef int all_set_bits, plus, minus
    all_set_bits = before | after
    plus = all_set_bits - before
    minus = all_set_bits - after
    return ((events | minus) - minus) | plus


# Note: this is a regular Python class to allow easy pickling.
class LineStats(object):
    """
    Object to encapsulate line-profile statistics.

    Attributes:

        timings (dict[tuple[str, int, str], \
list[tuple[int, int, int]]]):
            Mapping from ``(filename, first_lineno, function_name)`` of
            the profiled function to a list of
            ``(lineno, nhits, total_time)`` tuples for each profiled
            line. ``total_time`` is an integer in the native units of
            the timer.

        unit (float):
            The number of seconds per timer unit.
    """
    def __init__(self, timings, unit):
        self.timings = timings
        self.unit = unit


cdef class _SysMonitoringState:
    """
    Another helper object for managing the thread-local state.

    Note:
        Documentations are for reference only, and all APIs are to be
        considered private and subject to change.
    """
    cdef int tool_id
    cdef object name  # type: str | None
    # type: dict[int, Callable | None], int = event id
    cdef dict callbacks
    # type: dict[int, set[tuple[code, Unpack[tuple]]]],
    # int = event id, tuple = <locational info>
    cdef dict disabled
    cdef int events
    cdef Py_uintptr_t restart_version

    if _CAN_USE_SYS_MONITORING:
        line_tracing_event_set = (  # type: ClassVar[FrozenSet[int]]
            frozenset({sys.monitoring.events.LINE,
                       sys.monitoring.events.PY_RETURN,
                       sys.monitoring.events.PY_YIELD,
                       sys.monitoring.events.RAISE,
                       sys.monitoring.events.RERAISE}))
        line_tracing_events = (sys.monitoring.events.LINE
                               | sys.monitoring.events.PY_RETURN
                               | sys.monitoring.events.PY_YIELD
                               | sys.monitoring.events.RAISE
                               | sys.monitoring.events.RERAISE)
    else:
        line_tracing_event_set = frozenset({})
        line_tracing_events = 0

    def __init__(self, tool_id: int):
        self.tool_id = tool_id
        self.name = None
        self.callbacks = {}
        self.disabled = {}
        self.events = 0  # NO_EVENTS
        self.restart_version = monitoring_restart_version()

    cpdef register(self, object handle_line,
                   object handle_return, object handle_yield,
                   object handle_raise, object handle_reraise):
        # Note: only activating `sys.monitoring` line events for the
        # profiled code objects in `LineProfiler.add_function()` may
        # seem like an obvious optimization, but:
        # - That adds complexity and muddies the logic, because
        #   `.set_local_events()` can only be called if the tool id is
        #   in use (e.g. activated via `.use_tool_id()`), and
        # - That doesn't result in much (< 2%) performance improvement
        #   in tests
        mon = sys.monitoring

        # Set prior state
        # Note: in 3.14.0a1+, calling `sys.monitoring.free_tool_id()`
        # also calls `.clear_tool_id()`, causing existing callbacks and
        # code-object-local events to be wiped... so don't call free.
        # this does have the side effect of not overriding the active
        # profiling tool name if one is already in use, but it's
        # probably better this way
        self.name = mon.get_tool(self.tool_id)
        if self.name is None:
            self.events = mon.events.NO_EVENTS
            mon.use_tool_id(self.tool_id, 'line_profiler')
        else:
            self.events = mon.get_events(self.tool_id)
        mon.set_events(self.tool_id, self.events | self.line_tracing_events)

        # Register tracebacks and remember the existing ones
        for event_id, callback in [(mon.events.LINE, handle_line),
                                   (mon.events.PY_RETURN, handle_return),
                                   (mon.events.PY_YIELD, handle_yield),
                                   (mon.events.RAISE, handle_raise),
                                   (mon.events.RERAISE, handle_reraise)]:
            self.callbacks[event_id] = mon.register_callback(
                self.tool_id, event_id, callback)

    cpdef deregister(self):
        mon = sys.monitoring
        cdef dict wrapped_callbacks = self.callbacks

        # Restore prior state
        mon.set_events(self.tool_id, self.events)
        if self.name is None:
            mon.free_tool_id(self.tool_id)
        self.name = None
        self.events = mon.events.NO_EVENTS

        # Reset tracebacks
        while wrapped_callbacks:
            mon.register_callback(self.tool_id, *wrapped_callbacks.popitem())

    cdef void call_callback(self, int event_id, object code,
                            object loc_args, object other_args) noexcept:
        """
        Call the appropriate stored callback.  Also take care of the
        restoration of :py:mod:`sys.monitoring` callbacks, tool-ID lock,
        and events should they be unset.

        Note:
            * This is deliberately made a non-traceable C method so that
              we don't fall info infinite recursion.
            * ``loc_args`` and ``other_args`` should be tuples.
        """
        mon = sys.monitoring
        cdef PyObject *result = NULL
        cdef object callback  # type: Callable | None
        cdef object callback_after  # type: Callable | None
        cdef object code_location  # type: tuple[code, Unpack[tuple]]
        cdef object arg_tuple  # type: tuple[code, Unpack[tuple]]
        cdef object disabled  # type: set[tuple[code, Unpack[tuple]]]
        cdef int ev_id, events_before
        cdef Py_uintptr_t version = monitoring_restart_version()
        cdef dict callbacks_before = {}

        # If we've restarted events, clear the `.disabled` registry
        if version != self.restart_version:
            self.restart_version = version
            self.disabled.clear()

        # Call the wrapped callback where suitable
        callback = self.callbacks.get(event_id)
        if callback is None:  # No cached callback
            return
        code_location = (code,) + loc_args
        disabled = self.disabled.setdefault(event_id, set())
        if code_location in disabled:  # Events 'disabled' for the loc
            return
        if not (self.events  # Callback should not receive the event
                | mon.get_local_events(self.tool_id, code)) & event_id:
            return

        for ev_id in self.line_tracing_event_set:
            callbacks_before[ev_id] = get_current_callback(self.tool_id, ev_id)

        arg_tuple = code_location + other_args
        try:
            events_before = mon.get_events(self.tool_id)
            result = PyObject_Call(  # Note: DECREF needed below
                <PyObject *>callback, <PyObject *>arg_tuple, NULL)
        else:
            # Since we can't actually disable the event (or line
            # profiling will be interrupted), just mark the location so
            # that we stop calling the cached callback until the next
            # time `sys.monitoring.restart_events()` is called
            if result == <PyObject *>(mon.DISABLE):
                disabled.add(code_location)
        finally:
            Py_XDECREF(result)
            # Update the events
            self.events = _patch_events(
                self.events, events_before, mon.get_events(self.tool_id))
            # If the wrapped callback has changed:
            register = mon.register_callback
            for ev_id, callback in callbacks_before.items():
                # - Restore the `sys.monitoring` callback
                callback_after = register(self.tool_id, ev_id, callback)
                # - Remember the updated callback in `self.callbacks`
                if callback is not callback_after:
                    self.callbacks[ev_id] = callback_after
            # Reset the tool ID lock if released
            if not mon.get_tool(self.tool_id):
                mon.use_tool_id(self.tool_id, 'line_profiler')
            # Restore the `sys.monitoring` events if unset
            mon.set_events(self.tool_id,
                           self.events | self.line_tracing_events)


cdef class _LineProfilerManager:
    """
    Helper object for managing the thread-local state.
    Supports being called with the same signature as a legacy trace
    function (see :py:func:`sys.settrace`).

    Other methods of interest:

    :py:meth:`~.handle_line_event`
        Callback for |LINE|_ events
    :py:meth:`~.handle_return_event`
        Callback for |PY_RETURN|_ events
    :py:meth:`~.handle_yield_event`
        Callback for |PY_YIELD|_ events
    :py:meth:`~.handle_raise_event`
        Callback for |RAISE|_ events
    :py:meth:`~.handle_reraise_event`
        Callback for |RERAISE|_ events

    Note:
        Documentations are for reference only, and all APIs are to be
        considered private and subject to change.

    .. |LINE| replace:: :py:attr:`!sys.monitoring.events.LINE`
    .. |PY_RETURN| replace:: :py:attr:`!sys.monitoring.events.PY_RETURN`
    .. |PY_YIELD| replace:: :py:attr:`!sys.monitoring.events.PY_YIELD`
    .. |RAISE| replace:: :py:attr:`!sys.monitoring.events.RAISE`
    .. |RERAISE| replace:: :py:attr:`!sys.monitoring.events.RERAISE`
    .. _LINE: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-LINE
    .. _PY_RETURN: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-PY_RETURN
    .. _PY_YIELD: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-PY_YIELD
    .. _RAISE: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-RAISE
    .. _RERAISE: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-RERAISE
    """
    cdef TraceCallback *legacy_callback
    cdef _SysMonitoringState mon_state
    cdef public set active_instances  # type: set[LineProfiler]
    cdef int _wrap_trace
    cdef int _set_frame_local_trace
    cdef int recursion_guard

    def __init__(
            self, tool_id: int, wrap_trace: bool, set_frame_local_trace: bool):
        """
        Arguments:
            tool_id (int)
                Tool ID for use with :py:mod:`sys.monitoring`.
            wrap_trace (bool)
                Whether to wrap around legacy and
                :py:mod:`sys.monitoring` trace functions and call them.
            set_frame_local_trace (bool)
                If using the legacy trace system, whether to insert the
                instance as a frame's :py:attr:`~frame.f_trace` upon
                entering a function scope.

        See also:
            :py:class:`~.LineProfiler`
        """
        self.legacy_callback = NULL
        self.mon_state = _SysMonitoringState(tool_id)

        self.active_instances = set()
        self.wrap_trace = wrap_trace
        self.set_frame_local_trace = set_frame_local_trace
        self.recursion_guard = 0

    @cython.profile(False)
    def __call__(self, frame: types.FrameType, event: str, arg):
        """
        Calls |legacy_trace_callback|_.  If :py:func:`sys.gettrace`
        returns this instance, replaces the default C-level trace
        function |trace_trampoline|_ with |legacy_trace_callback|_ to
        reduce overhead.

        Returns:
            self (_LineProfilerManager):
                This instance.

        .. |legacy_trace_callback| replace:: \
:c:func:`!legacy_trace_callback`
        .. |trace_trampoline| replace:: :c:func:`!trace_trampoline`
        .. _legacy_trace_callback: https://github.com/pyutils/\
line_profiler/blob/main/line_profiler/_line_profiler.pyx
        .. _trace_trampoline: https://github.com/python/cpython/blob/\
6cb20a219a860eaf687b2d968b41c480c7461909/Python/sysmodule.c#L1124
        """
        cdef int what = {'call': PyTrace_CALL,
                         'exception': PyTrace_EXCEPTION,
                         'line': PyTrace_LINE,
                         'return': PyTrace_RETURN,
                         'opcode': PyTrace_OPCODE}[event]
        if not self.recursion_guard:
            # Prevent recursion (e.g. when `.wrap_trace` and
            # `.set_frame_local_trace` are both true)
            legacy_trace_callback(self, <PyFrameObject *>frame,
                                  what, <PyObject *>arg)
        # Set the C-level trace callback back to
        # `legacy_trace_callback()` where appropriate, so that future
        # calls can bypass this `.__call__()` method
        if sys.gettrace() is self:
            PyEval_SetTrace(legacy_trace_callback, self)
        return self

    def wrap_local_f_trace(self, trace_func: Callable) -> Callable:
        """
        Arguments:
            trace_func (Callable[[frame, str, Any], Any])
                Frame-local trace function, presumably set by another
                global trace function.

        Returns:
            wrapper (Callable[[frame, str, Any], Any])
                Thin wrapper around ``trace_func()`` which calls it,
                calls the instance, then returns the return value of
                ``trace_func()``.  This helps prevent other frame-local
                trace functions from displacing the instance when its
                :py:attr:`~.set_frame_local_trace` is true.

        Note:
            * The ``.__line_profiler_manager__`` attribute of the
              returned wrapper is set to the instance.
            * Line events are not passed to the wrapped callable if
              ``wrapper.disable_line_events`` is set to true.
        """
        @wraps(trace_func)
        def wrapper(frame, event, arg):
            if wrapper.disable_line_events and event == 'line':
                result = None
            else:
                result = trace_func(frame, event, arg)
            self(frame, event, arg)
            return result

        wrapper.__line_profiler_manager__ = self
        wrapper.disable_line_events = False
        try:  # Unwrap the wrapper
            if trace_func.__line_profiler_manager__ is self:
                trace_func = trace_func.__wrapped__
        except AttributeError:
            pass
        return wrapper

    # If we allowed these `sys.monitoring` callbacks to be profiled
    # (i.e. to emit line events), we may fall into an infinite recusion;
    # so disable profiling for them pre-emptively

    @cython.profile(False)
    cpdef handle_line_event(self, object code, int lineno):
        """
        Line-event (|LINE|_) callback passed to
        :py:func:`sys.monitoring.register_callback`.

        .. |LINE| replace:: :py:attr:`!sys.monitoring.events.LINE`
        .. _LINE: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-LINE
        """
        self._base_callback(
            1, sys.monitoring.events.LINE, code, lineno, (lineno,), ())

    @cython.profile(False)
    cpdef handle_return_event(
            self, object code, int instruction_offset, object retval):
        """
        Return-event (|PY_RETURN|_) callback passed to
        :py:func:`sys.monitoring.register_callback`.

        .. |PY_RETURN| replace:: \
:py:attr:`!sys.monitoring.events.PY_RETURN`
        .. _PY_RETURN: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-PY_RETURN
        """
        self._handle_exit_event(
            sys.monitoring.events.PY_RETURN, code, instruction_offset, retval)

    @cython.profile(False)
    cpdef handle_yield_event(
            self, object code, int instruction_offset, object retval):
        """
        Yield-event (|PY_YIELD|_) callback passed to
        :py:func:`sys.monitoring.register_callback`.

        .. |PY_YIELD| replace:: \
:py:attr:`!sys.monitoring.events.PY_YIELD`
        .. _PY_YIELD: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-PY_YIELD
        """
        self._handle_exit_event(
            sys.monitoring.events.PY_YIELD, code, instruction_offset, retval)

    @cython.profile(False)
    cpdef handle_raise_event(
            self, object code, int instruction_offset, object exception):
        """
        Raise-event (|RAISE|_) callback passed to
        :py:func:`sys.monitoring.register_callback`.

        .. |RAISE| replace:: :py:attr:`!sys.monitoring.events.RAISE`
        .. _RAISE: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-RAISE
        """
        self._handle_exit_event(
            sys.monitoring.events.RAISE, code, instruction_offset, exception)

    @cython.profile(False)
    cpdef handle_reraise_event(
            self, object code, int instruction_offset, object exception):
        """
        Re-raise-event (|RERAISE|_) callback passed to
        :py:func:`sys.monitoring.register_callback`.

        .. |RERAISE| replace:: :py:attr:`!sys.monitoring.events.RERAISE`
        .. _RERAISE: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-RERAISE
        """
        self._handle_exit_event(
            sys.monitoring.events.RERAISE, code, instruction_offset, exception)

    cdef void _handle_exit_event(
            self, int event_id, object code, int offset, object obj) noexcept:
        """
        Base for the frame-exit-event (e.g. via returning or yielding)
        callbacks passed to :py:func:`sys.monitoring.register_callback`.

        Note:
            This is deliberately made a non-traceable C method so that
            we don't fall info infinite recursion.
        """
        cdef int lineno = PyCode_Addr2Line(<PyCodeObject*>code, offset)
        self._base_callback(0, event_id, code, lineno, (offset,), (obj,))

    cdef void _base_callback(
            self, int is_line_event, int event_id, object code, int lineno,
            object loc_args, object other_args) noexcept:
        """
        Base for the various callbacks passed to
        :py:func:`sys.monitoring.register_callback`.

        Note:
            * This is deliberately made a non-traceable C method so that
              we don't fall info infinite recursion.
            * ``loc_args`` and ``other_args`` should be tuples.
        """
        inner_trace_callback(
            is_line_event, self.active_instances, code, lineno)
        if self._wrap_trace:
            self.mon_state.call_callback(event_id, code, loc_args, other_args)

    cpdef _handle_enable_event(self, prof):
        cdef TraceCallback* legacy_callback
        instances = self.active_instances
        already_active = bool(instances)
        instances.add(prof)
        if already_active:
            return
        if USE_LEGACY_TRACE:
            legacy_callback = alloc_callback()
            populate_callback(legacy_callback)
            self.legacy_callback = legacy_callback
            PyEval_SetTrace(legacy_trace_callback, self)
        else:
            self.mon_state.register(self.handle_line_event,
                                    self.handle_return_event,
                                    self.handle_yield_event,
                                    self.handle_raise_event,
                                    self.handle_reraise_event)

    cpdef _handle_disable_event(self, prof):
        cdef TraceCallback* legacy_callback
        instances = self.active_instances
        instances.discard(prof)
        if instances:
            return
        # Only use the legacy trace-callback system if Python < 3.12 or
        # if explicitly requested with `LINE_PROFILER_CORE=legacy`;
        # otherwise, use `sys.monitoring`
        # see: https://docs.python.org/3/library/sys.monitoring.html
        if USE_LEGACY_TRACE:
            legacy_callback = self.legacy_callback
            restore_callback(legacy_callback)
            free_callback(legacy_callback)
            self.legacy_callback = NULL
        else:
            self.mon_state.deregister()

    property wrap_trace:
        def __get__(self):
            return bool(self._wrap_trace)
        def __set__(self, wrap_trace):
            self._wrap_trace = 1 if wrap_trace else 0

    property set_frame_local_trace:
        def __get__(self):
            return bool(self._set_frame_local_trace)
        def __set__(self, set_frame_local_trace):
            # Note: as noted in `LineProfiler.__doc__`, there is no
            # point in tempering with `.f_trace` when using
            # `sys.monitoring`... so just set it to false
            self._set_frame_local_trace = (
                1 if set_frame_local_trace and USE_LEGACY_TRACE else 0)


cdef class LineProfiler:
    """
    Time the execution of lines of Python code.

    This is the Cython base class for
    :py:class:`line_profiler.line_profiler.LineProfiler`.

    Arguments:
        *functions (function)
            Function objects to be profiled.
        wrap_trace (bool | None)
            What to do for existing :py:mod:`sys` trace callbacks when
            an instance is :py:meth:`.enable`-ed:

            :py:data:`True`:
                *Wrap around* said callbacks: when our profiling trace
                callbacks run, they call the corresponding existing
                callbacks (where applicable).
            :py:data:`False`:
                *Suspend* said callbacks as long as
                :py:class:`LineProfiler` instances are enabled.
            :py:data:`None` (default):
                For the first instance created, resolves to

                :py:data:`True`
                    If the environment variable
                    :envvar:`LINE_PROFILER_WRAP_TRACE` is set to any of
                    ``{'1', 'on', 'true', 'yes'}`` (case-insensitive).

                :py:data:`False`
                    Otherwise.

                If other instances already exist, the value is inherited
                therefrom.

            In any case, when all instances are :py:meth:`.disable`-ed,
            the :py:mod:`sys` trace system is restored to the state from
            when the first instance was :py:meth:`.enable`-ed.
            See the :ref:`caveats <warning-trace-caveats>` and also the
            :ref:`extra explanation <note-wrap_trace>`.
        set_frame_local_trace (bool | None)
            When using the
            :ref:`"legacy" trace system <note-backends>`), what to do
            when entering a function or code block (i.e. an event of
            type :c:data:`PyTrace_CALL` or ``'call'`` is encountered)
            when an instance is :py:meth:`.enable`-ed:

            :py:data:`True`:
                Set the frame's :py:attr:`~frame.f_trace` to
                an object associated with the profiler.
            :py:data:`False`:
                Don't do so.
            :py:data:`None` (default):
                For the first instance created, resolves to

                :py:data:`True`
                    If the environment variable
                    :envvar:`LINE_PROFILER_SET_FRAME_LOCAL_TRACE` is set
                    to any of ``{'1', 'on', 'true', 'yes'}``
                    (case-insensitive).

                :py:data:`False`
                    Otherwise.

                If other instances already exist, the value is inherited
                therefrom.

            See the :ref:`caveats <warning-trace-caveats>` and also the
            :ref:`extra explanation <note-set_frame_local_trace>`.

    Example:
        >>> import copy
        >>> import line_profiler
        >>> # Create a LineProfiler instance
        >>> self = line_profiler.LineProfiler()
        >>> # Wrap a function
        >>> copy_fn = self(copy.copy)
        >>> # Call the function
        >>> copy_fn(self)
        >>> # Inspect internal properties
        >>> self.functions
        >>> self.c_last_time
        >>> self.c_code_map
        >>> self.code_map
        >>> self.last_time
        >>> # Print stats
        >>> self.print_stats()

    .. _warning-trace-caveats:

    Warning:
        * Setting :py:attr:`.wrap_trace` and/or
          :py:attr:`.set_frame_local_trace` helps with using
          :py:class:`LineProfiler` cooperatively with other tools, like
          coverage and debugging tools, especially when using the
          :ref:`"legacy" trace system <note-backends>`.  However, these
          parameters should be considered **experimental** and to be
          used at one's own risk -- because tools generally assume that
          they have sole control over system-wide tracing (if using
          "legacy" tracing), or at least over the
          :py:mod:`sys.monitoring` tool ID it acquired.
        * When setting :py:attr:`.wrap_trace` and
          :py:attr:`.set_frame_local_trace`, they are set process-wide
          for all instances.

    .. _note-backends:

    Note:
        There are two "cores"/"backends" for :py:class:`LineProfiler`
        between which users can choose:

        ``'new'``, ``'sys.monitoring'``, or ``'sysmon'``
            Use :py:mod:`sys.monitoring` events and callbacks.  Only
            available on (and is the default for) Python 3.12 and newer.
        ``'old'``, ``'legacy'``, or ``'ctrace'``
            Use the `"legacy" trace system`_ (:py:func:`sys.gettrace`,
            :py:func:`sys.settrace`, and :c:func:`PyEval_SetTrace`).
            Default for Python < 3.12.

        Where both cores are available, the user can choose between the
        two by supplying a suitable value to the environment variable
        :envvar:`LINE_PROFILER_CORE`.

    .. _note-wrap_trace:

    Note:
        More on :py:attr:`.wrap_trace`:

        * In general, Python allows for trace callbacks to unset
          themselves, either intentionally (via
          ``sys.settrace(None)`` or
          ``sys.monitoring.register_callback(..., None)``) or if they
          error out.  If the wrapped/cached trace callbacks do so,
          profiling would continue, but:

          * The cached callbacks are cleared and are no longer called,
            and
          * The trace callbacks are unset when all profiler instances
            are :py:meth:`.disable`-ed.
        * If a wrapped/cached frame-local
          :ref:`"legacy" trace callable <note-backends>`
          (:py:attr:`~frame.f_trace`) sets
          :py:attr:`~frame.f_trace_lines` to false in a frame to
          disable local line events, :py:attr:`~.frame.f_trace_lines`
          is restored (so that profiling can continue), but said
          callable will no longer receive said events.
        * Likewise, wrapped/cached :py:mod:`sys.monitoring` callbacks
          can also disable events:

          * At *specific code locations* by returning
            :py:data:`sys.monitoring.DISABLE`;
          * By calling :py:func:`sys.monitoring.set_events` and
            changing the *global event set*; or
          * By calling :py:func:`sys.monitoring.register_callback` and
            *replacing itself* with alternative callbacks (or
            :py:data:`None`).

          When that happens, said disabling acts are again suitably
          intercepted so that line profiling continues, but:

          * Said callbacks will no longer receive the corresponding
            events, and
          * The :py:mod:`sys.monitoring` callbacks and event set are
            updated correspondingly when all profiler instances are
            :py:meth:`.disable`-ed.

          Note that:

          * As with when line profiling is not used, if 
            :py:func:`sys.monitoring.restart_events` is called, the list
            of code locations where events are suppressed is cleared,
            and the wrapped/cached callbacks will once again receive
            events from the
            previously-:py:data:`~sys.monitoring.DISABLE`-d locations.
          * Callbacks which only listen to and alter code-object-local
            events (via :py:func:`sys.monitoring.set_local_events`) do
            not interfere with line profiling, and such changes are
            therefore not intercepted.

    .. _note-set_frame_local_trace:

    Note:
        More on :py:attr:`.set_frame_local_trace`:

        * Since frame-local trace functions is no longer a useful
          concept in the new :py:mod:`sys.monitoring`-based system
          (see also the :ref:`Note on "cores" <note-backends>`), the
          parameter/attribute always resolves to :py:data:`False` when
          using the new :py:class:`LineProfiler` core.
        * With the :ref:`"legacy" trace system <note-backends>`, when
          :py:class:`LineProfiler` instances are :py:meth:`.enable`-ed,
          :py:func:`sys.gettrace` returns an object which manages
          profiling on the thread between all active profiler instances.
          Said object has the same call signature as callables that
          :py:func:`sys.settrace` takes, so that pure-Python code which
          temporarily overrides the trace callable (e.g.
          :py:meth:`doctest.DocTestRunner.run`) can function with
          profiling.  After the object is restored with
          :py:func:`sys.settrace` by said code:

          * If :py:attr:`set_frame_local_trace` is true, line
            profiling resumes *immediately*, because the object has
            already been set to the frame's :py:attr:`~frame.f_trace`.
          * However, if :py:attr:`set_frame_local_trace` is false,
            line profiling only resumes *upon entering another code
            block* (e.g. by calling a callable), because trace
            callables set via :py:func:`sys.settrace` is only called
            for ``'call'`` events (see the `C implementation`_ of
            :py:mod:`sys`).

    .. _C implementation: https://github.com/python/cpython/blob/\
6cb20a219a860eaf687b2d968b41c480c7461909/Python/sysmodule.c#L1124
    .. _"legacy" trace system: https://github.com/python/cpython/blob/\
3.13/Python/legacy_tracing.c
    """
    cdef unordered_map[int64, LineTimeMap] _c_code_map
    # Mapping between thread-id and map of LastTime
    cdef unordered_map[int64, LastTimeMap] _c_last_time
    cdef public list functions
    cdef public dict code_hash_map, dupes_map
    cdef public double timer_unit
    cdef public object threaddata

    # These are shared between instances and threads
    if _CAN_USE_SYS_MONITORING:
        # Note: just in case we ever need to override this, e.g. for
        # testing
        tool_id = sys.monitoring.PROFILER_ID  # type: ClassVar[int]
    else:
        # Note: the value doesn't matter here... but set it to be
        # consistent (because the value is public API)
        tool_id = 2
    # type: ClassVar[dict[int, _LineProfilerManager]], int = thread id
    _managers = {}
    # type: ClassVar[dict[bytes, int]], bytes = bytecode
    _all_paddings = {}
    # type: ClassVar[dict[int, weakref.WeakSet[LineProfiler]]],
    # int = func id
    _all_instances_by_funcs = {}

    def __init__(self, *functions,
                 wrap_trace=None, set_frame_local_trace=None):
        self.functions = []
        self.code_hash_map = {}
        self.dupes_map = {}
        self.timer_unit = hpTimerUnit()
        # Create a data store for thread-local objects
        # https://docs.python.org/3/library/threading.html#thread-local-data
        self.threaddata = threading.local()
        if wrap_trace is not None:
            self.wrap_trace = wrap_trace
        if set_frame_local_trace is not None:
            self.set_frame_local_trace = set_frame_local_trace

        for func in functions:
            self.add_function(func)

    cpdef add_function(self, func):
        """
        Record line profiling information for the given Python function.

        Note:
            This is a low-level method and is intended for |function|_;
            users should in general use
            :py:meth:`line_profiler.LineProfiler.add_callable` for
            adding general callables and callable wrappers (e.g.
            :py:class:`property`).

        .. |function| replace:: :py:class:`types.FunctionType`
        .. _function: https://docs.python.org/3/reference/\
datamodel.html#user-defined-functions
        """
        if hasattr(func, "__wrapped__"):
            warn(
                "Adding a function with a `.__wrapped__` attribute. "
                "You may want to profile the wrapped function by adding "
                f"`{func.__name__}.__wrapped__` instead."
            )
        try:
            code = func.__code__
            func_id = id(func)
        except AttributeError:
            try:
                code = func.__func__.__code__
                func_id = id(func.__func__)
            except AttributeError:
                warn(
                    f"Could not extract a code object for the object {func!r}")
                return

        # Note: if we are to alter the code object, other profilers
        # which previously added this function would still expect the
        # old bytecode, and thus will not see anything when the function
        # is executed;
        # hence:
        # - When doing bytecode padding, take into account all instances
        #   which refers to the same base bytecode to ensure
        #   disambiguation
        # - Update all existing instances referring to the old code
        #   object
        # Since no code padding is/can be done with Cython mock
        # "code objects", it is *probably* okay to only do the special
        # handling on the non-Cython branch.
        # XXX: tests for the above assertion if necessary
        co_code: bytes = code.co_code
        code_hashes = []
        if any(co_code):  # Normal Python functions
            # Figure out how much padding we need and strip the bytecode
            base_co_code: bytes
            npad_code: int
            base_co_code, npad_code = multibyte_rstrip(co_code)
            try:
                npad = self._all_paddings[base_co_code]
            except KeyError:
                npad = 0
            self._all_paddings[base_co_code] = max(npad, npad_code) + 1
            try:
                profilers_to_update = self._all_instances_by_funcs[func_id]
                profilers_to_update.add(self)
            except KeyError:
                profilers_to_update = WeakSet({self})
                self._all_instances_by_funcs[func_id] = profilers_to_update
            # Maintain `.dupes_map` (legacy)
            try:
                self.dupes_map[base_co_code].append(code)
            except KeyError:
                self.dupes_map[base_co_code] = [code]
            if npad > npad_code:
                # Code hash already exists, so there must be a duplicate
                # function (on some instance);
                # (re-)pad with no-op
                co_code = base_co_code + NOP_BYTES * npad
                code = _code_replace(func, co_code)
                try:
                    func.__code__ = code
                except AttributeError as e:
                    func.__func__.__code__ = code
            else:  # No re-padding -> no need to update the other profs
                profilers_to_update = {self}
            # TODO: Since each line can be many bytecodes, this is kinda
            # inefficient
            # See if this can be sped up by not needing to iterate over
            # every byte
            for offset, _ in enumerate(co_code):
                code_hashes.append(
                    compute_line_hash(
                        hash(co_code),
                        PyCode_Addr2Line(<PyCodeObject*>code, offset)))
        else:  # Cython functions have empty/zero bytecodes
            if CANNOT_LINE_TRACE_CYTHON:
                return

            from line_profiler.line_profiler import get_code_block

            lineno = code.co_firstlineno
            if hasattr(func, '__code__'):
                cython_func = func
            else:
                cython_func = func.__func__
            cython_source = find_cython_source_file(cython_func)
            if not cython_source:  # Can't find the source
                return
            nlines = len(get_code_block(cython_source, lineno))
            block_hash = hash(code)
            for lineno in range(lineno, lineno + nlines):
                code_hash = compute_line_hash(block_hash, lineno)
                code_hashes.append(code_hash)
            # We can't replace the code object on Cython functions, but
            # we can *store* a copy with the correct metadata
            # Note: we don't use `_copy_local_sysmon_events()` here
            # because Cython shim code objects don't support local
            # events
            code = code.replace(co_filename=cython_source)
            profilers_to_update = {self}
        # Update `._c_code_map` and `.code_hash_map` with the new line
        # hashes on `self` (and other instances profiling the same
        # function if we padded the bytecode)
        for instance in profilers_to_update:
            prof = <LineProfiler>instance
            try:
                line_hashes = prof.code_hash_map[code]
            except KeyError:
                line_hashes = prof.code_hash_map[code] = []
            for code_hash in code_hashes:
                line_hash = <int64>code_hash
                if not prof._c_code_map.count(line_hash):
                    line_hashes.append(line_hash)
                    prof._c_code_map[line_hash]

        self.functions.append(func)

    property enable_count:
        def __get__(self):
            if not hasattr(self.threaddata, 'enable_count'):
                self.threaddata.enable_count = 0
            return self.threaddata.enable_count
        def __set__(self, value):
            self.threaddata.enable_count = value

    # These three are shared between instances, but thread-local
    # (Ideally speaking they could've been class attributes...)

    property wrap_trace:
        def __get__(self):
            return self._manager.wrap_trace
        def __set__(self, wrap_trace):
            # Make sure we have a manager
            manager = self._manager
            # Sync values between all thread states
            for manager in self._managers.values():
                manager.wrap_trace = wrap_trace

    property set_frame_local_trace:
        def __get__(self):
            return self._manager.set_frame_local_trace
        def __set__(self, set_frame_local_trace):
            # Make sure we have a manager
            manager = self._manager
            # Sync values between all thread states
            for manager in self._managers.values():
                manager.set_frame_local_trace = set_frame_local_trace

    property _manager:
        def __get__(self):
            thread_id = PyThread_get_thread_ident()
            try:
                return self._managers[thread_id]
            except KeyError:
                pass
            # First profiler instance on the thread, get the correct
            # `wrap_trace` and `set_frame_local_trace` values and set up
            # a `_LineProfilerManager`
            try:
                manager, *_ = self._managers.values()
            except ValueError:
                # First thread in the interpretor: load default values
                # from the environment (at package startup time)
                wrap_trace = WRAP_TRACE
                set_frame_local_trace = SET_FRAME_LOCAL_TRACE
            else:
                # Fetch the values from an existing manager
                wrap_trace = manager.wrap_trace
                set_frame_local_trace = manager.set_frame_local_trace
            self._managers[thread_id] = manager = _LineProfilerManager(
                self.tool_id, wrap_trace, set_frame_local_trace)
            return manager

    def enable_by_count(self):
        """ Enable the profiler if it hasn't been enabled before.
        """
        if self.enable_count == 0:
            self.enable()
        self.enable_count += 1

    def disable_by_count(self):
        """
        Disable the profiler if the number of disable requests matches
        (or exceeds) the number of enable requests.
        """
        if self.enable_count > 0:
            self.enable_count -= 1
            if self.enable_count == 0:
                self.disable()

    def __enter__(self):
        self.enable_by_count()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disable_by_count()

    def enable(self):
        self._manager._handle_enable_event(self)

    @property
    def c_code_map(self):
        """
        A Python view of the internal C lookup table.
        """
        return <dict>self._c_code_map

    @property
    def c_last_time(self):
        """
        Raises:
            KeyError
                If no profiling data is available on the current thread.
        """
        try:
            return (<dict>self._c_last_time)[PyThread_get_thread_ident()]
        except KeyError as e:
            # We haven't actually profiled anything yet
            raise (KeyError('No profiling data on the current thread '
                            '(`threading.get_ident()` = '
                            f'{threading.get_ident()})')
                   .with_traceback(e.__traceback__)) from None

    @property
    def code_map(self):
        """
        :py:mod:`line_profiler` 4.0 no longer directly maintains
        :py:attr:`~.code_map`, but this will construct something similar
        for backwards compatibility.
        """
        c_code_map = self.c_code_map
        code_hash_map = self.code_hash_map
        py_code_map = {}
        for code, code_hashes in code_hash_map.items():
            py_code_map.setdefault(code, {})
            for code_hash in code_hashes:
                c_entries = c_code_map[code_hash]
                py_entries = {}
                for key, c_entry in c_entries.items():
                    py_entry = c_entry.copy()
                    py_entry['code'] = code
                    py_entries[key] = py_entry
                py_code_map[code].update(py_entries)
        return py_code_map

    @property
    def last_time(self):
        """
        :py:mod:`line_profiler` 4.0 no longer directly maintains
        :py:attr:`~.last_time`, but this will construct something similar
        for backwards compatibility.
        """
        c_last_time = self.c_last_time
        py_last_time = {}
        for code in self.code_hash_map:
            block_hash = hash(code.co_code)
            if block_hash in c_last_time:
                py_last_time[code] = c_last_time[block_hash]
        return py_last_time

    cpdef disable(self):
        self._c_last_time[PyThread_get_thread_ident()].clear()
        self._manager._handle_disable_event(self)

    def get_stats(self):
        """
        Returns:
            :py:class:`LineStats` object containing the timings.
        """
        cdef dict cmap = self._c_code_map

        all_entries = {}
        for code in self.code_hash_map:
            entries = []
            for entry in self.code_hash_map[code]:
                entries.extend(cmap[entry].values())
            key = label(code)

            # Merge duplicate line numbers, which occur for branch
            # entrypoints like `if`
            entries_by_lineno = all_entries.setdefault(key, {})

            for line_dict in entries:
                 _, lineno, total_time, nhits = line_dict.values()
                 orig_nhits, orig_total_time = entries_by_lineno.get(
                     lineno, (0, 0))
                 entries_by_lineno[lineno] = (orig_nhits + nhits,
                                              orig_total_time + total_time)

        # Aggregate the timing data
        stats = {
            key: sorted((line, nhits, time)
                        for line, (nhits, time) in entries_by_lineno.items())
            for key, entries_by_lineno in all_entries.items()}
        return LineStats(stats, self.timer_unit)


@cython.boundscheck(False)
@cython.wraparound(False)
cdef inline void inner_trace_callback(
        int is_line_event, set instances, object code, int lineno):
    """
    The basic building block for the trace callbacks.
    """
    cdef LineProfiler prof_
    cdef LineProfiler prof
    cdef LastTime old
    cdef int key
    cdef PY_LONG_LONG time = 0
    cdef bint has_time = False
    cdef bint has_last
    cdef int64 code_hash
    cdef object py_bytes_obj = code.co_code
    cdef char* data = PyBytes_AS_STRING(py_bytes_obj)
    cdef Py_ssize_t size = PyBytes_GET_SIZE(py_bytes_obj)
    cdef unsigned long ident
    cdef Py_hash_t block_hash
    cdef LineTime* entry
    cdef LineTimeMap* line_entries
    cdef LastTimeMap* last_map

    # Loop over every byte to check if any are not NULL
    # if there are any non-NULL, that indicates we're profiling Python code
    for i in range(size):
        if data[i]:
            # because we use Python functions like hash, we CANNOT mark this function as nogil
            block_hash = hash(py_bytes_obj)
            break
    else:
        # fallback for Cython functions
        block_hash = hash(code)

    code_hash = compute_line_hash(block_hash, lineno)

    for prof_ in instances:
        # for some reason, doing this is much faster than just combining it into the above
        # like doing "for prof in instances:" is far slower
        prof = <LineProfiler>prof_
        if not prof._c_code_map.count(code_hash):
            continue
        if not has_time:
            time = hpTimer()
            has_time = True
        ident = PyThread_get_thread_ident()
        last_map = &(prof._c_last_time[ident])
        # deref() is Cython's version of the -> accessor in C++. if we don't use deref then
        # Cython thinks that when we index last_map,
        # we want pointer indexing (which is not the case)
        if deref(last_map).count(block_hash):
            old = deref(last_map)[block_hash]
            line_entries = &(prof._c_code_map[code_hash])
            # Ensure that an entry exists in line_entries before accessing it
            entry = line_ensure_entry(line_entries, old.f_lineno, code_hash)
            # Note: explicitly `deref()`-ing here causes the new values
            # to be assigned to a temp var;
            # meanwhile, directly dot-accessing a pointer causes Cython
            # to correctly write `ptr->attr = (ptr->attr + incr)`
            entry.nhits += 1
            entry.total_time += time - old.time
            has_last = True
        else:
            has_last = False
        if is_line_event:
            # Get the time again. This way, we don't record much time
            # wasted in this function.
            deref(last_map)[block_hash] = LastTime(lineno, hpTimer())
        elif deref(last_map).count(block_hash):
            # We are returning from a function, not executing a line.
            # Delete the last_time record. It may have already been
            # deleted if we are profiling a generator that is being
            # pumped past its end.
            last_erase_if_present(last_map, block_hash)


cdef extern int legacy_trace_callback(
        object manager, PyFrameObject *py_frame, int what, PyObject *arg):
    """
    The :c:func:`PyEval_SetTrace` callback.

    References:
       https://github.com/python/cpython/blob/de2a4036/Include/cpython/\
pystate.h#L16
    """
    cdef _LineProfilerManager manager_ = <_LineProfilerManager>manager
    cdef int result
    cdef int recursion_guard = manager_.recursion_guard
    cdef PyObject *code

    if what == PyTrace_CALL:
        # Any code using the `sys.gettrace()`-`sys.settrace()` paradigm
        # (e.g. to temporarily suspend or alter tracing) will cause line
        # events to not be passed to the global trace callback (i.e.
        # `manager`) for the rest of the frame, e.g.
        #     >>> callback = sys.gettrace()
        #     >>> sys.settrace(None)
        #     >>> try:  # Tracing suspended here
        #     ...     ...
        #     ... finally:
        #     ...     # Tracing object restored here, but line event
        #     ...     # tracing is disabled before the next function
        #     ...     # call because that's what the default trace
        #     ...     # trampoline works
        #     ...     sys.settrace(callback)
        # To circumvent this, set the local `.f_trace` upon entering a
        # frame (if not already set), so that tracing can restart upon
        # the restoration with `sys.settrace()`
        if manager_._set_frame_local_trace:
            set_local_trace(<PyObject *>manager_, py_frame)
    elif what == PyTrace_LINE or what == PyTrace_RETURN:
        code = <PyObject *>PyFrame_GetCode(py_frame)
        inner_trace_callback((what == PyTrace_LINE),
                             manager_.active_instances,
                             <object>code,
                             PyFrame_GetLineNumber(py_frame))
        Py_XDECREF(code)

    # Call the trace callback that we're wrapping around where
    # appropriate
    if manager_._wrap_trace:
        # Due to how the frame-local callback could be set to the active
        # `_LineProfilerManager` or a wrapper object (see
        # `set_local_trace()`), wrap the callback call to make sure that
        # we don't recurse back here
        manager_.recursion_guard = 1
        try:
            result = call_callback(
                <PyObject *>disable_line_events, manager_.legacy_callback,
                py_frame, what, arg)
        finally:
            manager_.recursion_guard = recursion_guard
    else:
        result = 0

    # Prevent other trace functions from overwritting `manager`;
    # if there is a frame-local trace function, create a wrapper calling
    # both it and `manager`
    if manager_._set_frame_local_trace:
        set_local_trace(<PyObject *>manager_, py_frame)
    return result
