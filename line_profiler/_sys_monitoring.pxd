# cython: language_level=3
# cython: legacy_implicit_noexcept=True
# used in _line_profiler.pyx

cdef extern from "c_trace_callbacks.h":
    ctypedef unsigned long long Py_uintptr_t


cdef class _SysMonitoringState:
    cdef int tool_id
    cdef object tool_id_obj  # (The same as a Python int)
    cdef object name  # type: str | None
    # The following are per line-tracing event (`LINE`, `PY_RETURN`,
    # `PY_YIELD`, `RAISE`, and `RERAISE`, in that order):
    # - Our callbacks; type: list[Callable | None]
    cdef list own_callbacks
    # - The callbacks we wrap around; type: list[Callable | None]
    cdef list callbacks
    # - The locations at which they returned `sys.monitoring.DISABLE`;
    #   type: list[set[tuple[code, int]] | None]
    cdef list disabled
    cdef int events
    cdef Py_uintptr_t restart_version

    cpdef register(self, object handle_line,
                   object handle_return, object handle_yield,
                   object handle_raise, object handle_reraise)
    cpdef deregister(self)
    cdef void call_callback(self, int event_id, object code, int loc,
                            object arg, bint has_arg) noexcept
