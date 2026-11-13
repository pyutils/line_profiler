# cython: language_level=3
# cython: infer_types=True
# cython: legacy_implicit_noexcept=True
# distutils: language=c++
# distutils: include_dirs = python25.pxd
r"""
Bookkeeping for wrapping around other tools' sys.monitoring
callbacks, used by line_profiler._line_profiler.

Note:
    This is split from line_profiler._line_profiler so that the
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
