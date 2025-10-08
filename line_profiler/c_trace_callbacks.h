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
 *   therefrom which are used by `pycore_interp.h`, and its dependencies
 *   `pycore_ceval_state.h` and `pycore_gil.h` (or at least mock them)
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
#           define Py_ATOMIC_H
            // Used in `pycore_interp.h`
            typedef struct _Py_atomic_address {
                volatile uintptr_t _value;
            } _Py_atomic_address;
            // Used in `pycore_gil.h` and `pycore_ceval_state.h`
            typedef struct _Py_atomic_int {
                volatile int _value;
            } _Py_atomic_int;
            /* Stub out macros in `pycore_atomic.h` used in macros in
             * `pycore_interp.h` (which aren't related to the
             * `struct _is` we need).
             * If any stub is referenced, fail the build with an
             * unresolved external.
             * This ensures we never ship wheels that "use" these
             * placeholders. */
#           ifdef _MSC_VER
                __declspec(dllimport) void lp_link_error__stubbed_cpython_atomic_LOAD_relaxed_was_used_this_is_a_bug(void);
                __declspec(dllimport) void lp_link_error__stubbed_cpython_atomic_STORE_relaxed_was_used_this_is_a_bug(void);
#           else
                extern void lp_link_error__stubbed_cpython_atomic_LOAD_relaxed_was_used_this_is_a_bug(void);
                extern void lp_link_error__stubbed_cpython_atomic_STORE_relaxed_was_used_this_is_a_bug(void);
#           endif
#           define _LP_ATOMIC_PANIC_LOAD_EXPR()  (lp_link_error__stubbed_cpython_atomic_LOAD_relaxed_was_used_this_is_a_bug(), 0)
#           define _LP_ATOMIC_PANIC_STORE_STMT() do { lp_link_error__stubbed_cpython_atomic_STORE_relaxed_was_used_this_is_a_bug(); } while (0)
            // Panic-on-use shims (expression/statement forms)
#           undef  _Py_atomic_load_relaxed
#           undef  _Py_atomic_store_relaxed
#           define _Py_atomic_load_relaxed(obj)       ((void)(obj), _LP_ATOMIC_PANIC_LOAD_EXPR())
#           define _Py_atomic_store_relaxed(obj, val)  do { (void)(obj); (void)(val); _LP_ATOMIC_PANIC_STORE_STMT(); } while (0)
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
