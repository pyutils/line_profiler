#ifndef LINE_PROFILER_C_TRACE_CALLBACKS_H
#define LINE_PROFILER_C_TRACE_CALLBACKS_H

#include "Python_wrapper.h"
#include "frameobject.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct TraceCallback
{
    /* Notes:
     *     - These fields are synonymous with the corresponding fields
     *       in a `PyThreadState` object;
     *       however, note that `PyThreadState.c_tracefunc` is
     *       considered a CPython implementation detail.
     *     - It is necessary to reach into the thread-state internals
     *       like this, because `sys.gettrace()` only retrieves
     *       `.c_traceobj`, and is thus only valid for Python-level
     *       trace callables set via `sys.settrace()` (which implicitly
     *       sets `.c_tracefunc` to
     *       `Python/sysmodule.c::trace_trampoline()`).
     */
    Py_tracefunc c_tracefunc;
    PyObject *c_traceobj;
} TraceCallback;

TraceCallback *alloc_callback();
void free_callback(TraceCallback *callback);
void populate_callback(TraceCallback *callback);
void restore_callback(TraceCallback *callback);
int call_callback(
    PyObject *disabler,
    TraceCallback *callback,
    PyFrameObject *py_frame,
    int what,
    PyObject *arg
);
void set_local_trace(PyObject *manager, PyFrameObject *py_frame);
Py_uintptr_t monitoring_restart_version();

#ifdef __cplusplus
}
#endif

#endif // LINE_PROFILER_C_TRACE_CALLBACKS_H
