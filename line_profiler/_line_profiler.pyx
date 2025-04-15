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
    typedef struct CTraceState {
        /* Notes:
         *     - These are synonymous with the corresponding fields in a
         *       `PyThreadState` object;
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
    } CTraceState;

    CTraceState *fetch_c_trace_state() {
        /* Returns:
         *     `malloc()`-ed pointer to a `CTraceState`, which contains
         *     the members `.c_tracefunc` and `.c_traceobj` of the
         *     current thread.
         */
        // No need to `Py_DECREF()` the thread state, since it isn't a
        // `PyObject`
        PyThreadState *thread_state = PyThreadState_Get();
        CTraceState *trace_state = (CTraceState*)malloc(sizeof(CTraceState));
        trace_state->c_tracefunc = thread_state->c_tracefunc;
        trace_state->c_traceobj = thread_state->c_traceobj;
        // No need for NULL check with `Py_XINCREF()`
        Py_XINCREF(trace_state->c_traceobj);
        return trace_state;
    }

    int c_trace_state_has_hook(CTraceState *state) {
        /* Returns:
         *     Whether the pointer `state` is non-`NULL` and points
         *     towards a `CTraceState` with non-`NULL` `.c_tracefunc`
         *     and `.c_traceobj`
         */
        // No stored state
        if (state == NULL) return 0;
        // There is a stored state, but no associated tracer
        if (state->c_tracefunc == NULL || state->c_traceobj == NULL) return 0;
        return 1;
    }

    void restore_c_trace_state(CTraceState *state) {
        /* If `state` is a non-`NULL` pointer to a `CTraceState`:
         * - Use `PyEval_SetTrace()` to set the trace callback on the
         *   current thread to be consistent with the `state`, and
         * - `free()`-s the `state`
         */
        // No-op if there isn't a stored state
        if (state == NULL) return;
        PyEval_SetTrace(state->c_tracefunc, state->c_traceobj);
        // No need for NULL check with `Py_XDECREF()`
        Py_XDECREF(state->c_traceobj);
        free(state);
        return;
    }

    int call_c_trace_state_hook(CTraceState *state,
                                PyFrameObject *py_frame,
                                int what,
                                PyObject *arg) {
        /* Returns:
         *     0 if `state` doesn't correspond to a valid and non-`null`
         *     trace callback, the result of calling said callback
         *     otherwise
         *
         * Notes:
         *     Use the Cython wrapper `call_c_trace_state_hook_safe()`
         *     instead of this directly to avoid having the `state`
         *     callback interfering with the current `sys` trace
         *     callback.
         */
        if (!c_trace_state_has_hook(state)) return 0;
        return (state->c_tracefunc)(state->c_traceobj, py_frame, what, arg);
    }

    inline PyObject *get_optional_attr(PyObject *obj, const char *attr) {
        /* Returns:
         *     `<PyObject *>getattr(obj, attr)` if `obj` has `attr`,
         *     `NULL` otherwise
         *
         * Notes:
         *     Ref-counts are not managed here;
         *     the function is inlined and we let Cython deal with that.
         */
        if (!PyObject_HasAttrString(obj, attr)) return NULL;
        return PyObject_GetAttrString(obj, attr);
    }

    inline int set_optional_attr(PyObject *obj,
                                 const char *attr,
                                 PyObject *value) {
        /* Returns:
         *     - 0 if `value` is `NULL` and `obj` doesn't have `attr`
         *     - The result of `PyObject_DelAttrString(obj, attr)` if
         *       `value` is `NULL`
         *     - The result of
         *       `PyObject_SetAttrString(obj, attr, value)` otherwise
         *
         * Notes:
         *     Ref-counts are not managed here;
         *     the function is inlined and we let Cython deal with that.
         */
        int hasattr = PyObject_HasAttrString(obj, attr);
        if (!hasattr && value == NULL) return 0;  // No-op
        if (value == NULL) return PyObject_DelAttrString(obj, attr);
        return PyObject_SetAttrString(obj, attr, value);
    }
    """
    ctypedef struct CTraceState:
        Py_tracefunc c_tracefunc
        PyObject *c_traceobj

    cdef CTraceState *fetch_c_trace_state()
    cdef int c_trace_state_has_hook(CTraceState *state)
    cdef void restore_c_trace_state(CTraceState *state)
    cdef int call_c_trace_state_hook(CTraceState *state,
                                     PyFrameObject *py_frame,
                                     int what,
                                     PyObject *arg)
    cdef PyObject *get_optional_attr(PyObject *obj, const char *attr)
    cdef int set_optional_attr(PyObject *obj, const char *attr, PyObject *value)

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
            `.enable()`-ed.

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
    """
    cdef unordered_map[int64, unordered_map[int64, LineTime]] _c_code_map
    # Mapping between thread-id and map of LastTime
    cdef unordered_map[int64, unordered_map[int64, LastTime]] _c_last_time
    cdef CTraceState *_prev_trace_state
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
        _sys_monitoring_register()
        self._prev_trace_state = fetch_c_trace_state()
        PyEval_SetTrace(python_trace_callback, self)

    @property
    def wrap_trace(self):
        return bool(self._wrap_trace)

    @wrap_trace.setter
    def wrap_trace(self, wrap_trace):
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
        self._c_last_time[threading.get_ident()].clear()
        restore_c_trace_state(self._prev_trace_state)
        self._prev_trace_state = NULL
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


cdef int call_c_trace_state_hook_safe(CTraceState *state,
                                      PyFrameObject *py_frame,
                                      int what, PyObject *arg):
    """
    Call the cached trace callback `state` where appropriate, and in a
    "safe" way so that the following therein are guarded against:
    1. Altering of the `sys` trace callback
    2. Altering of the frame's `.f_trace_lines`
    The latter is particularly important, since line events are turned
    off if `.f_trace_lines = False`, effectively neutering line
    profiling.

    Returns:
        0 if `state` doesn't correspond to a valid and non-`null` trace
        callback, the result of calling said callback otherwise

    Notes:
        - Against (1), it may seem to suffice to set
          `PyEval_SetTrace(python_trace_callback, self)`;
          however, that presumes that this trace function is always the
          "active" one, instead of being possibly wrapped by another
          function, like how it wraps around the old trace callback.
          Hence we do a fetch-restore cycle of the `CTraceState` here.
        - For (2), Cython manages the refcounts for the
          `get_optional_attr()` results, like it does for
          `get_frame_code()` in `python_trace_callback()`, so it
          shouldn't be necessary to call `Py_[X]DECREF()` thereon.
        - Unlike `.f_trace_lines`, `.f_trace` is not restored since
          frame-local event tracing for Python-level tracing callbacks
          (which uses `Python/sysmodule.c::trace_trampoline()`) depends
          on it being set.
    """
    cdef CTraceState *current_state
    cdef PyObject *f_obj, *f_trace_lines

    f_obj = <PyObject *>py_frame

    if not c_trace_state_has_hook(state):  # No existing callback
        return 0

    current_state = fetch_c_trace_state()
    f_trace_lines = get_optional_attr(f_obj, 'f_trace_lines')

    result = call_c_trace_state_hook(state, py_frame, what, arg)

    restore_c_trace_state(current_state)
    if get_optional_attr(f_obj, 'f_trace_lines') != f_trace_lines:
        set_optional_attr(f_obj, 'f_trace_lines', f_trace_lines)

    return result


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

    if what == PyTrace_LINE or what == PyTrace_RETURN:
        # Normally we'd need to DECREF the return from get_frame_code, but Cython does that for us
        block_hash = hash(get_frame_code(py_frame))

        linenum = PyFrame_GetLineNumber(py_frame)
        code_hash = compute_line_hash(block_hash, linenum)
        
        if self._c_code_map.count(code_hash):
            time = hpTimer()
            ident = threading.get_ident()
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
        return call_c_trace_state_hook_safe(self._prev_trace_state,
                                            py_frame, what, arg)
    return 0
