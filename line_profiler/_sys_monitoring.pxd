# cython: language_level=3
# cython: legacy_implicit_noexcept=True
# used in _line_profiler.pyx

cdef extern from "c_trace_callbacks.h":
    ctypedef unsigned long long Py_uintptr_t


cdef class _SysMonitoringState:
    cdef int tool_id
    cdef object name  # type: str | None
    # type: dict[int, Callable | None], int = event id
    cdef dict callbacks
    # type: dict[int, set[tuple[code, Unpack[tuple]]]],
    # int = event id, tuple = <locational info>
    cdef dict disabled
    cdef int events
    cdef Py_uintptr_t restart_version

    cpdef register(self, object handle_line,
                   object handle_return, object handle_yield,
                   object handle_raise, object handle_reraise)
    cpdef deregister(self)
    cdef void call_callback(self, int event_id, object code,
                            object loc_args, object other_args) noexcept
