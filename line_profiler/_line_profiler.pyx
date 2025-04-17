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
from .python25 cimport PyFrameObject, PyObject, PyStringObject
from collections.abc import Callable
from functools import wraps
from sys import byteorder
import sys
cimport cython
from cpython.version cimport PY_VERSION_HEX
from libc.stdint cimport int64_t

from libcpp.unordered_map cimport unordered_map
import threading
import opcode

NOP_VALUE: int = opcode.opmap['NOP']

# The Op code should be 2 bytes as stated in
# https://docs.python.org/3/library/dis.html
# if sys.version_info[0:2] >= (3, 11):
NOP_BYTES: bytes = NOP_VALUE.to_bytes(2, byteorder=byteorder)

# long long int is at least 64 bytes assuming c99
ctypedef unsigned long long int uint64
ctypedef long long int int64

# FIXME: there might be something special we have to do here for Python 3.11
cdef extern from "frameobject.h":
    """
    inline PyObject* get_frame_code(PyFrameObject* frame) {
        #if PY_VERSION_HEX < 0x030B0000
            Py_INCREF(frame->f_code->co_code);
            return frame->f_code->co_code;
        #else
            PyCodeObject* code = PyFrame_GetCode(frame);
            PyObject* ret = PyCode_GetCode(code);
            Py_DECREF(code);
            return ret;
        #endif
    }
    """
    cdef object get_frame_code(PyFrameObject* frame)
    ctypedef int (*Py_tracefunc)(object self, PyFrameObject *py_frame, int what, PyObject *arg)

cdef extern from "Python.h":
    """
    // CPython 3.11 broke some stuff by moving PyFrameObject :(
    #if PY_VERSION_HEX >= 0x030b00a6
      #ifndef Py_BUILD_CORE
        #define Py_BUILD_CORE 1
      #endif
      #include "internal/pycore_frame.h"
      #include "cpython/code.h"
      #include "pyframe.h"
    #endif
    """
    ctypedef struct PyFrameObject
    ctypedef struct PyCodeObject
    ctypedef long long PY_LONG_LONG
    cdef bint PyCFunction_Check(object obj)
    cdef int PyCode_Addr2Line(PyCodeObject *co, int byte_offset)

    cdef void PyEval_SetProfile(Py_tracefunc func, object arg)
    cdef void PyEval_SetTrace(Py_tracefunc func, object arg)

    ctypedef object (*PyCFunction)(object self, object args)

    ctypedef struct PyMethodDef:
        char *ml_name
        PyCFunction ml_meth
        int ml_flags
        char *ml_doc

    ctypedef struct PyCFunctionObject:
        PyMethodDef *m_ml
        PyObject *m_self
        PyObject *m_module

    # They're actually #defines, but whatever.
    cdef int PyTrace_CALL
    cdef int PyTrace_EXCEPTION
    cdef int PyTrace_LINE
    cdef int PyTrace_RETURN
    cdef int PyTrace_C_CALL
    cdef int PyTrace_C_EXCEPTION
    cdef int PyTrace_C_RETURN

    cdef int PyFrame_GetLineNumber(PyFrameObject *frame)

cdef extern from *:
    """
    typedef struct TraceCallback {
        /* Notes:
         *     - These fields are synonymous with the corresponding
         *       fields in a `PyThreadState` object;
         *       however, note that `PyThreadState.c_tracefunc` is
         *       considered a CPython implementation detail.
         *     - It is necessary to reach into the thread-state
         *       internals like this, because `sys.gettrace()` only
         *       retrieves `.c_traceobj`, and is thus only valid for
         *       Python-level trace callables set via `sys.settrace()`
         *       (which implicitly sets `.c_tracefunc` to
         *       `Python/sysmodule.c::trace_trampoline()`).
         */
        Py_tracefunc c_tracefunc; PyObject *c_traceobj;
    } TraceCallback;

    TraceCallback *alloc_callback() {
        /* Heap-allocate a new `TraceCallback`. */
        return (TraceCallback*)malloc(sizeof(TraceCallback));
    }

    void free_callback(TraceCallback *callback) {
        /* Free a heap-allocated `TraceCallback`. */
        if (callback != NULL) free(callback);
        return;
    }

    void fetch_callback(TraceCallback *callback) {
        /* Store the members `.c_tracefunc` and `.c_traceobj` of the
         * current thread on `callback`.
         */
        // Shouldn't happen, but just to be safe
        if (callback == NULL) return;
        // No need to `Py_DECREF()` the thread callback, since it isn't
        // a `PyObject`
        PyThreadState *thread_state = PyThreadState_Get();
        callback->c_tracefunc = thread_state->c_tracefunc;
        callback->c_traceobj = thread_state->c_traceobj;
        // No need for NULL check with `Py_XINCREF()`
        Py_XINCREF(callback->c_traceobj);
        return;
    }

    void nullify_callback(TraceCallback *callback) {
        // No need for NULL check with `Py_XDECREF()`
        Py_XDECREF(callback->c_traceobj);
        callback->c_tracefunc = NULL;
        callback->c_traceobj = NULL;
        return;
    }

    void restore_callback(TraceCallback *callback) {
        /* Use `PyEval_SetTrace()` to set the trace callback on the
         * current thread to be consistent with the `callback`, then
         * nullify the pointers on `callback`.
         */
        // Shouldn't happen, but just to be safe
        if (callback == NULL) return;
        PyEval_SetTrace(callback->c_tracefunc, callback->c_traceobj);
        nullify_callback(callback);
        return;
    }

    inline int is_null_callback(TraceCallback *callback) {
        return (callback == NULL
                || callback->c_tracefunc == NULL
                || callback->c_traceobj == NULL);
    }

    int call_callback(TraceCallback *callback, PyFrameObject *py_frame,
                      int what, PyObject *arg) {
        /* Call the cached trace callback `callback` where appropriate,
         * and in a "safe" way so that:
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
         *       callback is preserved but `callback` itself is
         *       nullified.
         *       This is to comply with what Python usually does: if the
         *       trace callback errors out, `sys.settrace(None)` is
         *       called.
         *     - If a frame-local callback sets the `.f_trace_lines` to
         *       false, `.f_trace_lines` is reverted but `.f_trace` is
         *       wrapped so that it no loger sees line events.
         *
         * Notes:
         *     It is tempting to assume said current callback value to
         *     be `{ python_trace_callback, <profiler> }`, but remember
         *     that our callback may very well be called via another
         *     callback, much like how we call the cached callback via
         *     `python_trace_callback()`.
         */
        TraceCallback before, after;
        PyObject *mod = NULL, *dle = NULL, *f_trace = NULL;
        char f_trace_lines;
        int result;

        if (is_null_callback(callback)) return 0;

        f_trace_lines = py_frame->f_trace_lines;
        fetch_callback(&before);
        result = (callback->c_tracefunc)(
            callback->c_traceobj, py_frame, what, arg);

        // Check if the callback has unset itself; if so, nullify
        // `callback`
        fetch_callback(&after);
        if (is_null_callback(&after)) nullify_callback(callback);
        nullify_callback(&after);
        restore_callback(&before);

        // Check if a frame-local callback has disabled future line
        // events, and revert the change in such a case (while
        // withholding future line events from the callback)
        if (!(py_frame->f_trace_lines)
                && f_trace_lines != py_frame->f_trace_lines) {
            py_frame->f_trace_lines = f_trace_lines;
            if (py_frame->f_trace != NULL && py_frame->f_trace != Py_None) {
                mod = PyImport_ImportModule("line_profiler._line_profiler");
                if (mod == NULL) {
                    PyErr_SetString(PyExc_ImportError,
                                    "cannot import "
                                    "`line_profiler._line_profiler`");
                    result = -1;
                    goto cleanup;
                }
                dle = PyObject_GetAttrString(mod, "disable_line_events");
                if (dle == NULL) {
                    PyErr_SetString(PyExc_AttributeError,
                                    "`line_profiler._line_profiler` has no "
                                    "attribute `disable_line_events`");
                    result = -1;
                    goto cleanup;
                }
                // Note: don't DECREF the pointer! Nothing else is
                // holding a reference to it.
                f_trace = PyObject_CallFunctionObjArgs(dle, py_frame->f_trace,
                                                       NULL);
                if (f_trace == NULL) {
                    // No need to raise another exception, it's already
                    // raised in the call
                    result = -1;
                    goto cleanup;
                }
                py_frame->f_trace = f_trace;
            }
        }
    cleanup:
        Py_XDECREF(mod);
        Py_XDECREF(dle);
        return result;
    }
    """
    ctypedef struct TraceCallback:
        Py_tracefunc c_tracefunc
        PyObject *c_traceobj

    cdef TraceCallback *alloc_callback()
    cdef void free_callback(TraceCallback *callback)
    cdef void fetch_callback(TraceCallback *callback)
    cdef void restore_callback(TraceCallback *callback)
    cdef int call_callback(TraceCallback *callback, PyFrameObject *py_frame,
                           int what, PyObject *arg)

cdef extern from "timers.c":
    PY_LONG_LONG hpTimer()
    double hpTimerUnit()

cdef struct LineTime:
    int64 code
    int lineno
    PY_LONG_LONG total_time
    long nhits

cdef struct LastTime:
    int f_lineno
    PY_LONG_LONG time

cdef inline int64 compute_line_hash(uint64 block_hash, uint64 linenum):
    """
    Compute the hash used to store each line timing in an unordered_map.
    This is fairly simple, and could use some improvement since linenum
    isn't technically random, however it seems to be good enough and
    fast enough for any practical purposes.
    """
    # linenum doesn't need to be int64 but it's really a temporary value
    # so it doesn't matter
    return block_hash ^ linenum


if PY_VERSION_HEX < 0x030c00b1:  # 3.12.0b1

    def _sys_monitoring_register() -> None: 
        ...

    def _sys_monitoring_deregister() -> None: 
        ...

else:

    def _is_main_thread() -> bool:
        return threading.current_thread() == threading.main_thread()

    def _sys_monitoring_register() -> None:
        if not _is_main_thread():
            return
        mon = sys.monitoring
        mon.use_tool_id(mon.PROFILER_ID, 'line_profiler')

    def _sys_monitoring_deregister() -> None:
        if not _is_main_thread():
            return
        mon = sys.monitoring
        mon.free_tool_id(mon.PROFILER_ID)

def label(code):
    """
    Return a (filename, first_lineno, func_name) tuple for a given code object.

    This is the same labelling as used by the cProfile module in Python 2.5.
    """
    if isinstance(code, str):
        return ('~', 0, code)    # built-in functions ('~' sorts at the end)
    else:
        return (code.co_filename, code.co_firstlineno, code.co_name)


def disable_line_events(trace_func: Callable) -> Callable:
    """
    Return a thin wrapper around `trace_func()` which withholds line
    events. This is for when a frame-local `.f_trace` disables
    `.f_trace_lines` -- we would like to keep line events enabled (so
    that line profiling works) while "unsubscribing" the trace function
    from it.
    """
    @wraps(trace_func)
    def wrapper(frame, event, args):
        if event == 'line':
            return
        return trace_func(frame, event, args)

    return wrapper


cpdef _code_replace(func, co_code):
    """
    Implements CodeType.replace for Python < 3.8
    """
    try:
        code = func.__code__
    except AttributeError:
        code = func.__func__.__code__
    if hasattr(code, 'replace'):
        # python 3.8+
        code = code.replace(co_code=co_code)
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


# Note: this is a regular Python class to allow easy pickling.
class LineStats(object):
    """
    Object to encapsulate line-profile statistics.

    Attributes:

        timings (dict):
            Mapping from (filename, first_lineno, function_name) of the
            profiled function to a list of (lineno, nhits, total_time) tuples
            for each profiled line. total_time is an integer in the native
            units of the timer.

        unit (float):
            The number of seconds per timer unit.
    """
    def __init__(self, timings, unit):
        self.timings = timings
        self.unit = unit


cdef class LineProfiler:
    """
    Time the execution of lines of Python code.

    This is the Cython base class for
    :class:`line_profiler.line_profiler.LineProfiler`.

    Arguments:
        *functions (types.FunctionType)
            Function objects to be profiled.
        wrap_trace (Optional[bool])
            What to do if there is an existing (non-profiling) `sys`
            trace callback when the profiler is `.enable()`-ed:
            True:
                WRAP AROUND said callback: at the end of running our
                trace callback, also run the existing callback.
            False:
                REPLACE said callback as long as the profiler is
                enabled.
            None (default):
                If the environment variable `${LINE_PROFILE_WRAP_TRACE}`
                is undefined, or if it matches any of
                `{'', '0', 'off', 'false', 'no'}` (case-insensitive):
                  -> `False`;
                Otherwise
                  -> `True`.
            In any case, when the profiler is `.disable()`-ed, it tries
            to restore the `sys` trace callback (or the lack thereof) to
            the state it was in from when the profiler was
            `.enable()`-ed (but see Notes).

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

    Notes:
        - `wrap_trace = True` helps with using `LineProfiler`
          cooperatively with other tools, like coverage and debugging
          tools.
        - However, it should be considered experimental and to be used
          at one's own risk -- because tools generally assume that they
          have sole control over system-wide tracing.
        - In general, Python allows for trace callbacks to unset
          themselves, either intentionally (via `sys.settrace(None)`) or
          if it errors out. If the wrapped/cached trace callback does
          so, profiling would continue, but:
          - The cached callback is cleared and is no longer called, and
          - The `sys` trace callback is set to `None` when the profiler
            is `.disable()`-ed.
        - It is also allowed for the frame-local trace callable
          (`.f_trace`) to set `.f_trace_lines` to false in a frame to
          disable line events. If the wrapped/cached trace callback does
          so, profiling would continue, but `.f_trace` will no longer
          receive line events.
    """
    cdef unordered_map[int64, unordered_map[int64, LineTime]] _c_code_map
    # Mapping between thread-id and map of LastTime
    cdef unordered_map[int64, unordered_map[int64, LastTime]] _c_last_time
    # Mapping between thread-id and the thread-local tracing callbacks
    # (It would've been cleaner to do this as a property using
    # `.threaddata` like `.enable_count`, but I can't seem to figure out
    # how to cast Cython pointers to/from Python integers...)
    cdef unordered_map[int64, TraceCallback *] _trace_callbacks
    cdef public int _wrap_trace
    cdef public list functions
    cdef public dict code_hash_map, dupes_map
    cdef public double timer_unit
    cdef public object threaddata

    def __init__(self, *functions, wrap_trace=None):
        self.functions = []
        self.code_hash_map = {}
        self.dupes_map = {}
        self.timer_unit = hpTimerUnit()
        # Create a data store for thread-local objects
        # https://docs.python.org/3/library/threading.html#thread-local-data
        self.threaddata = threading.local()
        if wrap_trace is None:
            import os
            wrap_trace = (os.environ.get('LINE_PROFILE_WRAP_TRACE', '').lower()
                          not in {'', '0', 'off', 'false', 'no'})
        self.wrap_trace = wrap_trace

        for func in functions:
            self.add_function(func)

    cpdef add_function(self, func):
        """ Record line profiling information for the given Python function.
        """
        if hasattr(func, "__wrapped__"):
            import warnings
            warnings.warn(
                "Adding a function with a __wrapped__ attribute. You may want "
                "to profile the wrapped function by adding %s.__wrapped__ "
                "instead." % (func.__name__,)
            )
        try:
            code = func.__code__
        except AttributeError:
            try:
                code = func.__func__.__code__
            except AttributeError:
                import warnings
                warnings.warn("Could not extract a code object for the object %r" % (func,))
                return

        if code.co_code in self.dupes_map:
            self.dupes_map[code.co_code] += [code]
            # code hash already exists, so there must be a duplicate function. add no-op
            co_padding : bytes = NOP_BYTES * (len(self.dupes_map[code.co_code]) + 1)
            co_code = code.co_code + co_padding
            CodeType = type(code)
            code = _code_replace(func, co_code=co_code)
            try:
                func.__code__ = code
            except AttributeError as e:
                func.__func__.__code__ = code
        else:
            self.dupes_map[code.co_code] = [code]
        # TODO: Since each line can be many bytecodes, this is kinda inefficient
        # See if this can be sped up by not needing to iterate over every byte
        for offset, byte in enumerate(code.co_code):
            code_hash = compute_line_hash(hash((code.co_code)), PyCode_Addr2Line(<PyCodeObject*>code, offset))
            if not self._c_code_map.count(code_hash):
                try:
                    self.code_hash_map[code].append(code_hash)
                except KeyError:
                    self.code_hash_map[code] = [code_hash]
                self._c_code_map[code_hash]

        self.functions.append(func)

    property enable_count:
        def __get__(self):
            if not hasattr(self.threaddata, 'enable_count'):
                self.threaddata.enable_count = 0
            return self.threaddata.enable_count
        def __set__(self, value):
            self.threaddata.enable_count = value

    def enable_by_count(self):
        """ Enable the profiler if it hasn't been enabled before.
        """
        if self.enable_count == 0:
            self.enable()
        self.enable_count += 1

    def disable_by_count(self):
        """ Disable the profiler if the number of disable requests matches the
        number of enable requests.
        """
        if self.enable_count > 0:
            self.enable_count -= 1
            if self.enable_count == 0:
                self.disable()

    def __enter__(self):
        self.enable_by_count()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disable_by_count()

    cpdef enable(self):
        # Register `line_profiler` with `sys.monitoring` in Python 3.12
        # and above;
        # see: https://docs.python.org/3/library/sys.monitoring.html
        cdef TraceCallback *callback = alloc_callback()
        _sys_monitoring_register()
        self._trace_callbacks[threading.get_ident()] = callback
        fetch_callback(callback)
        PyEval_SetTrace(python_trace_callback, self)

    property wrap_trace:
        def __get__(self):
            return bool(self._wrap_trace)
        def __set__(self, wrap_trace):
            self._wrap_trace = 1 if wrap_trace else 0

    @property
    def c_code_map(self):
        """
        A Python view of the internal C lookup table.
        """
        return <dict>self._c_code_map

    @property
    def c_last_time(self):
        return (<dict>self._c_last_time)[threading.get_ident()]

    @property
    def code_map(self):
        """
        line_profiler 4.0 no longer directly maintains code_map, but this will
        construct something similar for backwards compatibility.
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
        line_profiler 4.0 no longer directly maintains last_time, but this will
        construct something similar for backwards compatibility.
        """
        c_last_time = (<dict>self._c_last_time)[threading.get_ident()]
        code_hash_map = self.code_hash_map
        py_last_time = {}
        for code, code_hashes in code_hash_map.items():
            for code_hash in code_hashes:
                if code_hash in c_last_time:
                    py_last_time[code] = c_last_time[code_hash]
        return py_last_time


    cpdef disable(self):
        cdef int64 ident = threading.get_ident()
        cdef TraceCallback *callback = self._trace_callbacks[ident]
        self._c_last_time[ident].clear()
        restore_callback(callback)
        free_callback(callback)
        self._trace_callbacks[ident] = NULL
        # Deregister `line_profiler` with `sys.monitoring` in Python
        # 3.12 and above;
        # see: https://docs.python.org/3/library/sys.monitoring.html
        _sys_monitoring_deregister()

    def get_stats(self):
        """
        Return a LineStats object containing the timings.
        """
        cdef dict cmap = self._c_code_map

        stats = {}
        for code in self.code_hash_map:
            entries = []
            for entry in self.code_hash_map[code]:
                entries += list(cmap[entry].values())
            key = label(code)

            # Merge duplicate line numbers, which occur for branch entrypoints like `if`
            nhits_by_lineno = {}
            total_time_by_lineno = {}

            for line_dict in entries:
                 _, lineno, total_time, nhits = line_dict.values()
                 nhits_by_lineno[lineno] = nhits_by_lineno.setdefault(lineno, 0) + nhits
                 total_time_by_lineno[lineno] = total_time_by_lineno.setdefault(lineno, 0) + total_time

            entries = [(lineno, nhits, total_time_by_lineno[lineno]) for lineno, nhits in nhits_by_lineno.items()]
            entries.sort()

            # NOTE: v4.x may produce more than one entry per line. For example:
            #   1:  for x in range(10):
            #   2:      pass
            #  will produce a 1-hit entry on line 1, and 10-hit entries on lines 1 and 2
            #  This doesn't affect `print_stats`, because it uses the last entry for a given line (line number is
            #  used a dict key so earlier entries are overwritten), but to keep compatability with other tools,
            #  let's only keep the last entry for each line
            # Remove all but the last entry for each line
            entries = list({e[0]: e for e in entries}.values())
            stats[key] = entries
        return LineStats(stats, self.timer_unit)


@cython.boundscheck(False)
@cython.wraparound(False)
cdef extern int python_trace_callback(object self_, PyFrameObject *py_frame,
                                      int what, PyObject *arg):
    """
    The PyEval_SetTrace() callback.

    References:
       https://github.com/python/cpython/blob/de2a4036/Include/cpython/pystate.h#L16 
    """
    cdef LineProfiler self
    cdef LastTime old
    cdef int key
    cdef PY_LONG_LONG time
    cdef int64 code_hash
    cdef int64 block_hash
    cdef unordered_map[int64, LineTime] line_entries
    cdef uint64 linenum

    self = <LineProfiler>self_
    ident = threading.get_ident()

    if what == PyTrace_LINE or what == PyTrace_RETURN:
        # Normally we'd need to DECREF the return from get_frame_code, but Cython does that for us
        block_hash = hash(get_frame_code(py_frame))

        linenum = PyFrame_GetLineNumber(py_frame)
        code_hash = compute_line_hash(block_hash, linenum)
        
        if self._c_code_map.count(code_hash):
            time = hpTimer()
            if self._c_last_time[ident].count(block_hash):
                old = self._c_last_time[ident][block_hash]
                line_entries = self._c_code_map[code_hash]
                key = old.f_lineno
                if not line_entries.count(key):
                    self._c_code_map[code_hash][key] = LineTime(code_hash, key, 0, 0)
                self._c_code_map[code_hash][key].nhits += 1
                self._c_code_map[code_hash][key].total_time += time - old.time
            if what == PyTrace_LINE:
                # Get the time again. This way, we don't record much time wasted
                # in this function.
                self._c_last_time[ident][block_hash] = LastTime(linenum, hpTimer())
            elif self._c_last_time[ident].count(block_hash):
                # We are returning from a function, not executing a line. Delete
                # the last_time record. It may have already been deleted if we
                # are profiling a generator that is being pumped past its end.
                self._c_last_time[ident].erase(self._c_last_time[ident].find(block_hash))

    # Call the trace callback that we're wrapping around where
    # appropriate
    if self._wrap_trace:
        return call_callback(self._trace_callbacks[ident], py_frame, what, arg)
    return 0
