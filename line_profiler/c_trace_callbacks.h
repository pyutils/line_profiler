#ifndef LINE_PROFILER_C_TRACE_CALLBACKS_H
#define LINE_PROFILER_C_TRACE_CALLBACKS_H

#include "Python_wrapper.h"
#include "frameobject.h"

/*
 * XXX: would make better sense to declare `PyInterpreterState` in
 * "Python_wrapper.h", but the file declaring it causes all sorts of
 * trouble across various platforms and Python versions... so
 * - Only include the file if we are actually using it here, i.e. in
 *   3.12+, and
 * - Undefine the `_PyGC_FINALIZED()` macro which is removed in 3.13+
 *   and causes problems in 3.12 (see CPython #105268, #105350, #107348)
 * - Undefine the `HAVE_STD_ATOMIC` macro, which causes problems on
 *   Linux in 3.12 (see CPython #108216)
 * - Set `Py_ATOMIC_H` to true to circumvent the #include of
 *   `include/pycore_atomic.h` (in `include/pycore_interp.h`, so that
 *   problematic function definitions therein are replaced with dummy
 *   ones (see #390); note that we still need to vendor in parts
 *   therefrom which are used by `pycore_interp.h` (or at least mock
 *   them)
 * Note in any case that we don't actually use `PyInterpreterState`
 * directly -- we just need its memory layout so that we can refer to
 * its `.last_restart_version` member
 */

// _is -> PyInterpreterState
#if PY_VERSION_HEX >= 0x030c00b1  // 3.12.0b6
#   ifndef Py_BUILD_CORE
#       define Py_BUILD_CORE 1
#   endif
#   if PY_VERSION_HEX < 0x030d0000  // 3.13
#       undef _PyGC_FINALIZED
#       ifdef __linux__
#           undef HAVE_STD_ATOMIC
#       endif
#       if (defined(_M_ARM) || defined(_M_ARM64)) && (! defined(Py_ATOMIC_H))
            typedef struct _Py_atomic_address {
                volatile uintptr_t _value;
            } _Py_atomic_address;
#           define _Py_atomic_load_relaxed(foo) (0)
#           define _Py_atomic_store_relaxed(foo, bar) (0)
#           include "internal/pycore_interp.h"
#       endif
#   endif
#   include "internal/pycore_interp.h"
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

#endif // LINE_PROFILER_C_TRACE_CALLBACKS_H
