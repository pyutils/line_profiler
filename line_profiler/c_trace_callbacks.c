#include "c_trace_callbacks.h"

/*
 * XXX: would make better sense to declare `PyInterpreterState` in
 * "Python_wrapper.h", but the file declaring it causes all sorts of
 * trouble across various platforms and Python versions... so we handle it here.
 *
 * Note that we don't actually use `PyInterpreterState` directly -- we just
 * need its memory layout so that we can refer to its `.last_restart_version` member.
 */

// _is -> PyInterpreterState
#if PY_VERSION_HEX >= 0x030c00b1  // 3.12.0b6
#   ifndef Py_BUILD_CORE
#       define Py_BUILD_CORE 1
#   endif
#   if PY_VERSION_HEX < 0x030d0000  // 3.13
        /*
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
         */
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

#define CYTHON_MODULE "line_profiler._line_profiler"
#define DISABLE_CALLBACK "disable_line_events"
#define RAISE_IN_CALL(func_name, xc, const_msg) \
    PyErr_SetString(xc, \
                    "in `" CYTHON_MODULE "." func_name "()`: " \
                    const_msg)

TraceCallback *alloc_callback()
{
    /* Heap-allocate a new `TraceCallback`. */
    TraceCallback *callback = (TraceCallback*)malloc(sizeof(TraceCallback));
    if (callback == NULL) RAISE_IN_CALL(
        // If we're here we have bigger fish to fry... but be nice and
        // raise an error explicitly anyway
        "alloc_callback",
        PyExc_MemoryError,
        "failed to allocate memory for storing the existing "
        "`sys` trace callback"
    );
    return callback;
}

void free_callback(TraceCallback *callback)
{
    /* Free a heap-allocated `TraceCallback`. */
    if (callback != NULL) free(callback);
    return;
}

void populate_callback(TraceCallback *callback)
{
    /* Store the members `.c_tracefunc` and `.c_traceobj` of the
     * current thread on `callback`.
     */
    // Shouldn't happen, but just to be safe
    if (callback == NULL) return;
    // No need to `Py_DECREF()` the thread callback, since it isn't a
    // `PyObject`
    PyThreadState *thread_state = PyThreadState_Get();
    callback->c_tracefunc = thread_state->c_tracefunc;
    callback->c_traceobj = thread_state->c_traceobj;
    // No need for NULL check with `Py_XINCREF()`
    Py_XINCREF(callback->c_traceobj);
    return;
}

void nullify_callback(TraceCallback *callback)
{
    if (callback == NULL) return;
    // No need for NULL check with `Py_XDECREF()`
    Py_XDECREF(callback->c_traceobj);
    callback->c_tracefunc = NULL;
    callback->c_traceobj = NULL;
    return;
}

void restore_callback(TraceCallback *callback)
{
    /* Use `PyEval_SetTrace()` to set the trace callback on the current
     * thread to be consistent with the `callback`, then nullify the
     * pointers on `callback`.
     */
    // Shouldn't happen, but just to be safe
    if (callback == NULL) return;
    PyEval_SetTrace(callback->c_tracefunc, callback->c_traceobj);
    nullify_callback(callback);
    return;
}

static inline int is_null_callback(TraceCallback *callback)
{
    return (
        callback == NULL
        || callback->c_tracefunc == NULL
        || callback->c_traceobj == NULL
    );
}

int call_callback(
    PyObject *disabler,
    TraceCallback *callback,
    PyFrameObject *py_frame,
    int what,
    PyObject *arg
)
{
    /* Call the cached trace callback `callback` where appropriate, and
     * in a "safe" way so that:
     * - If it alters the `sys` trace callback, or
     * - If it sets `.f_trace_lines` to false,
     * said alterations are reverted so as not to hinder profiling.
     *
     * Returns:
     *     - 0 if `callback` is `NULL` or has nullified members;
     *     - -1 if an error occurs (e.g. when the disabling of line
     *       events for the frame-local trace function failed);
     *     - The result of calling said callback otherwise.
     *
     * Side effects:
     *     - If the callback unsets the `sys` callback, the `sys`
     *       callback is preserved but `callback` itself is nullified.
     *       This is to comply with what Python usually does: if the
     *       trace callback errors out, `sys.settrace(None)` is called.
     *     - If a frame-local callback sets the `.f_trace_lines` to
     *       false, `.f_trace_lines` is reverted but `.f_trace` is
     *       wrapped/altered so that it no longer sees line events.
     *
     * Notes:
     *     It is tempting to assume said current callback value to be
     *     `{ python_trace_callback, <profiler> }`, but remember that
     *     our callback may very well be called via another callback,
     *     much like how we call the cached callback via
     *     `python_trace_callback()`.
     */
    TraceCallback before, after;
    PyObject *mod = NULL, *dle = NULL, *f_trace = NULL;
    char f_trace_lines;
    int result;

    if (is_null_callback(callback)) return 0;

    f_trace_lines = py_frame->f_trace_lines;
    populate_callback(&before);
    result = (callback->c_tracefunc)(
        callback->c_traceobj, py_frame, what, arg
    );

    // Check if the callback has unset itself; if so, nullify `callback`
    populate_callback(&after);
    if (is_null_callback(&after)) nullify_callback(callback);
    nullify_callback(&after);
    restore_callback(&before);

    // Check if a callback has disabled future line events for the
    // frame, and if so, revert the change while withholding future line
    // events from the callback
    if (
        !(py_frame->f_trace_lines)
        && f_trace_lines != py_frame->f_trace_lines
    )
    {
        py_frame->f_trace_lines = f_trace_lines;
        if (py_frame->f_trace != NULL && py_frame->f_trace != Py_None)
        {
            // Note: DON'T `Py_[X]DECREF()` the pointer! Nothing else is
            // holding a reference to it.
            f_trace = PyObject_CallOneArg(disabler, py_frame->f_trace);
            if (f_trace == NULL)
            {
                // No need to raise another exception, it's already
                // raised in the call
                result = -1;
                goto cleanup;
            }
            // No need to raise another exception, it's already
            // raised in the call
            if (PyObject_SetAttrString(
                (PyObject *)py_frame, "f_trace", f_trace))
            {
                result = -1;
            }
        }
    }
cleanup:
    Py_XDECREF(mod);
    Py_XDECREF(dle);
    return result;
}

void set_local_trace(PyObject *manager, PyFrameObject *py_frame)
{
    /* Set the frame-local trace callable:
     * - If there isn't one already, set it to `manager`;
     * - Else, call manager.wrap_local_f_trace()` on `py_frame->f_trace`
     *   where appropriate, setting the frame-local trace callable.
     *
     * Notes:
     *     This function is necessary for side-stepping Cython's auto
     *     memory management, which causes the return value of
     *     `wrap_local_f_trace()` to trigger the "Casting temporary
     *     Python object to non-numeric non-Python type" error.
     */
    PyObject *method = NULL;
    if (manager == NULL || py_frame == NULL) goto cleanup;
    // No-op
    if (py_frame->f_trace == manager) goto cleanup;
    // No local trace function to wrap, just assign `manager`
    if (py_frame->f_trace == NULL || py_frame->f_trace == Py_None)
    {
        Py_INCREF(manager);
        py_frame->f_trace = manager;
        goto cleanup;
    }
    // Wrap the trace function
    // (No need to raise another exception in case the call or the
    // `setattr()` failed, it's already raised in the call)
    method = PyUnicode_FromString("wrap_local_f_trace");
    PyObject_SetAttrString(
        (PyObject *)py_frame, "f_trace",
        PyObject_CallMethodOneArg(manager, method, py_frame->f_trace));
cleanup:
    Py_XDECREF(method);
    return;
}

Py_uintptr_t monitoring_restart_version()
#if PY_VERSION_HEX >= 0x030c00b1  // 3.12.0b1
{
    /* Get the `.last_restart_version` of the interpretor state.
     */
    return PyThreadState_GetInterpreter(
        PyThreadState_Get())->last_restart_version;
}
#else
{ return (Py_uintptr_t)0; }  // Dummy implementation
#endif
