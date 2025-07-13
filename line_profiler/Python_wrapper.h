// Compatibility layer over `Python.h`.

#ifndef LINE_PROFILER_PYTHON_WRAPPER_H
#define LINE_PROFILER_PYTHON_WRAPPER_H

#include "Python.h"

// Ensure PyFrameObject availability as a concretely declared struct

// _frame -> PyFrameObject
#if PY_VERSION_HEX >= 0x030b00a6  // 3.11.0a6
#   ifndef Py_BUILD_CORE
#       define Py_BUILD_CORE 1
#   endif
#   include "internal/pycore_frame.h"
#   include "cpython/code.h"
#   include "pyframe.h"
#else
#   include "frameobject.h"
#endif

// Backport of Python 3.9 caller hooks

#if PY_VERSION_HEX < 0x030900a4  // 3.9.0a4
#   define PyObject_CallOneArg(func, arg) \
        PyObject_CallFunctionObjArgs(func, arg, NULL)
#   define PyObject_CallMethodOneArg(obj, name, arg) \
        PyObject_CallMethodObjArgs(obj, name, arg, NULL)
#   define PyObject_CallNoArgs(func) \
        PyObject_CallFunctionObjArgs(func, NULL)
#   define PyObject_CallMethodNoArgs(obj, name) \
        PyObject_CallMethodObjArgs(obj, name, NULL)
#endif

#if PY_VERSION_HEX < 0x030900a5  // 3.9.0a5
#   define PyThreadState_GetInterpreter(tstate) \
        ((tstate)->interp)
#endif

#if PY_VERSION_HEX < 0x030900b1  // 3.9.0b1
    /*
     * Notes:
     *     While 3.9.0a1 already has `PyFrame_GetCode()`, it doesn't
     *     INCREF the code object until 0b1 (PR #19773), so override
     *     that for consistency.
     */
#   define PyFrame_GetCode(x) PyFrame_GetCode_backport(x)
    inline PyCodeObject *PyFrame_GetCode_backport(PyFrameObject *frame)
    {
        PyCodeObject *code;
        assert(frame != NULL);
        code = frame->f_code;
        assert(code != NULL);
        Py_INCREF(code);
        return code;
    }
#endif

#if PY_VERSION_HEX < 0x030b00b1  // 3.11.0b1
    /*
     * Notes:
     *     Since 3.11.0a7 (PR #31888) `co_code` has been made a
     *     descriptor, so:
     *     - This already creates a NewRef, so don't INCREF in that
     *       case; and
     *     - `code->co_code` will not work.
     */
    inline PyObject *PyCode_GetCode(PyCodeObject *code)
    {
        PyObject *code_bytes;
        if (code == NULL) return NULL;
#       if PY_VERSION_HEX < 0x030b00a7  // 3.11.0a7
            code_bytes = code->co_code;
            Py_XINCREF(code_bytes);
#       else
            code_bytes = PyObject_GetAttrString(code, "co_code");
#       endif
        return code_bytes;
    }
#endif

#if PY_VERSION_HEX < 0x030d00a1  // 3.13.0a1
    inline PyObject *PyImport_AddModuleRef(const char *name)
    {
        PyObject *mod = NULL, *name_str = NULL;
        name_str = PyUnicode_FromString(name);
        if (name_str == NULL) goto cleanup;
        mod = PyImport_AddModuleObject(name_str);
        Py_XINCREF(mod);
    cleanup:
        Py_XDECREF(name_str);
        return mod;
    }
#endif

#endif // LINE_PROFILER_PYTHON_WRAPPER_H
