# cython: language_level=3
# cython: infer_types=True
# cython: legacy_implicit_noexcept=True
# distutils: language=c++
# distutils: include_dirs = python25.pxd
r"""
Bookkeeping for wrapping around other tools' :py:mod:`sys.monitoring`
callbacks, used by :py:mod:`line_profiler._line_profiler`.

Note:
    This is split from :py:mod:`line_profiler._line_profiler` so that the
    extension modules can be compiled in parallel; none of this is on
    the hot path unless other tools' callbacks are being wrapped.
"""
import sys
from cpython.version cimport PY_VERSION_HEX

cdef extern from "Python_wrapper.h":
    ctypedef struct PyObject

    cdef PyObject* PyObject_Call(
        PyObject *callable, PyObject *args, PyObject *kwargs) except *
    cdef void Py_XDECREF(PyObject *o)

cdef extern from "c_trace_callbacks.h":
    cdef Py_uintptr_t monitoring_restart_version()

# Note: same as line_profiler._line_profiler._CAN_USE_SYS_MONITORING
_CAN_USE_SYS_MONITORING = PY_VERSION_HEX >= 0x030c00b1


# The `sys.monitoring` API and constants, looked up once here rather than
# on every call of `_SysMonitoringState.call_callback()`
cdef object _register_callback = None
cdef object _get_events = None
cdef object _set_events = None
cdef object _get_local_events = None
cdef object _get_tool = None
cdef object _use_tool_id = None
cdef object _free_tool_id = None
cdef object _DISABLE = None
# The events used for line profiling, in the order of the arguments of
# `_SysMonitoringState.register()`
cdef int _NUM_LINE_TRACING_EVENTS = 5
cdef int _LINE_TRACING_EVENT_IDS[5]
# The same as Python ints, so that they don't have to be created anew
# for every call into `sys.monitoring`
cdef tuple _LINE_TRACING_EVENT_ID_OBJS = ()
cdef int _LINE_TRACING_EVENTS = 0
if _CAN_USE_SYS_MONITORING:
    _register_callback = sys.monitoring.register_callback
    _get_events = sys.monitoring.get_events
    _set_events = sys.monitoring.set_events
    _get_local_events = sys.monitoring.get_local_events
    _get_tool = sys.monitoring.get_tool
    _use_tool_id = sys.monitoring.use_tool_id
    _free_tool_id = sys.monitoring.free_tool_id
    _DISABLE = sys.monitoring.DISABLE
    for _i, _event_id in enumerate([sys.monitoring.events.LINE,
                                    sys.monitoring.events.PY_RETURN,
                                    sys.monitoring.events.PY_YIELD,
                                    sys.monitoring.events.RAISE,
                                    sys.monitoring.events.RERAISE]):
        _LINE_TRACING_EVENT_IDS[_i] = _event_id
        _LINE_TRACING_EVENTS |= _event_id
        _LINE_TRACING_EVENT_ID_OBJS += (_event_id,)
    del _i, _event_id


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


cdef class _SysMonitoringState:
    """
    Another helper object for managing the thread-local state.

    Note:
        Documentations are for reference only, and all APIs are to be
        considered private and subject to change.
    """
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
        self.tool_id_obj = tool_id
        self.name = None
        self.own_callbacks = [None] * _NUM_LINE_TRACING_EVENTS
        self.callbacks = [None] * _NUM_LINE_TRACING_EVENTS
        self.disabled = [None] * _NUM_LINE_TRACING_EVENTS
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
        cdef int i

        # Set prior state
        # Note: in 3.14.0a1+, calling `sys.monitoring.free_tool_id()`
        # also calls `.clear_tool_id()`, causing existing callbacks and
        # code-object-local events to be wiped... so don't call free.
        # this does have the side effect of not overriding the active
        # profiling tool name if one is already in use, but it's
        # probably better this way
        self.name = _get_tool(self.tool_id)
        if self.name is None:
            self.events = 0  # NO_EVENTS
            _use_tool_id(self.tool_id, 'line_profiler')
        else:
            self.events = _get_events(self.tool_id)
        _set_events(self.tool_id, self.events | _LINE_TRACING_EVENTS)

        # Register tracebacks and remember the existing ones
        self.own_callbacks = [handle_line, handle_return, handle_yield,
                              handle_raise, handle_reraise]
        for i in range(_NUM_LINE_TRACING_EVENTS):
            self.callbacks[i] = _register_callback(
                self.tool_id, _LINE_TRACING_EVENT_IDS[i],
                self.own_callbacks[i])

    cpdef deregister(self):
        cdef int i

        # Restore prior state
        _set_events(self.tool_id, self.events)
        if self.name is None:
            _free_tool_id(self.tool_id)
        self.name = None
        self.events = 0  # NO_EVENTS

        # Reset tracebacks
        for i in range(_NUM_LINE_TRACING_EVENTS):
            _register_callback(self.tool_id, _LINE_TRACING_EVENT_IDS[i],
                               self.callbacks[i])
            self.callbacks[i] = None
            self.own_callbacks[i] = None

    cdef void call_callback(self, int event_id, object code, int loc,
                            object arg, bint has_arg) noexcept:
        """
        Call the appropriate stored callback with ``(code, loc)`` (or
        ``(code, loc, arg)`` if ``has_arg`` is true).  Also take care of
        the restoration of :py:mod:`sys.monitoring` callbacks, tool-ID
        lock, and events should they be unset.

        Note:
            This is deliberately made a non-traceable C method so that
            we don't fall info infinite recursion.
        """
        cdef PyObject *result = NULL
        cdef object callback  # type: Callable | None
        cdef object callback_after  # type: Callable | None
        cdef object code_location  # type: tuple[code, int]
        cdef object arg_tuple  # type: tuple[code, Unpack[tuple]]
        cdef set disabled  # type: set[tuple[code, int]] | None
        cdef int i, index = -1, events_before = 0, events_after
        cdef bint reacquired = False
        cdef Py_uintptr_t version = monitoring_restart_version()

        for i in range(_NUM_LINE_TRACING_EVENTS):
            if _LINE_TRACING_EVENT_IDS[i] == event_id:
                index = i
                break
        if index < 0:
            return

        # If we've restarted events, clear the `.disabled` registry
        if version != self.restart_version:
            self.restart_version = version
            for i in range(_NUM_LINE_TRACING_EVENTS):
                self.disabled[i] = None

        # Call the wrapped callback where suitable
        callback = self.callbacks[index]
        if callback is None:  # No cached callback
            return
        code_location = (code, loc)
        disabled = self.disabled[index]
        if disabled is not None and code_location in disabled:
            return  # Events 'disabled' for the loc
        # Callback should not receive the event (only check the local
        # events if the global ones don't include it)
        if not (self.events & event_id):
            if not (_get_local_events(self.tool_id_obj, code) & event_id):
                return

        arg_tuple = (code, loc, arg) if has_arg else code_location
        try:
            events_before = _get_events(self.tool_id_obj)
            result = PyObject_Call(  # Note: DECREF needed below
                <PyObject *>callback, <PyObject *>arg_tuple, NULL)
        else:
            # Since we can't actually disable the event (or line
            # profiling will be interrupted), just mark the location so
            # that we stop calling the cached callback until the next
            # time `sys.monitoring.restart_events()` is called
            if result == <PyObject *>_DISABLE:
                if disabled is None:
                    disabled = self.disabled[index] = set()
                disabled.add(code_location)
        finally:
            Py_XDECREF(result)
            # Update the events
            events_after = _get_events(self.tool_id_obj)
            self.events = _patch_events(
                self.events, events_before, events_after)
            # If the wrapped callback has replaced any of our callbacks:
            # - Restore ours, and
            # - Remember the replacement as the callback to wrap
            for i in range(_NUM_LINE_TRACING_EVENTS):
                callback_after = _register_callback(
                    self.tool_id_obj, _LINE_TRACING_EVENT_ID_OBJS[i],
                    self.own_callbacks[i])
                if callback_after is not self.own_callbacks[i]:
                    self.callbacks[i] = callback_after
            # Reset the tool ID lock if released
            if not _get_tool(self.tool_id_obj):
                _use_tool_id(self.tool_id_obj, 'line_profiler')
                reacquired = True
            # Restore the `sys.monitoring` events if unset
            if reacquired or events_after != (
                    self.events | _LINE_TRACING_EVENTS):
                _set_events(self.tool_id_obj,
                            self.events | _LINE_TRACING_EVENTS)
