from python25 cimport PyFrameObject, PyObject, PyStringObject
cimport cython

from libcpp.unordered_map cimport unordered_map

ctypedef unsigned long long int uint64
ctypedef long long int int64

# FIXME: there might be something special we have to do here for Python 3.11
cdef extern from "frameobject.h":
    ctypedef int (*Py_tracefunc)(object self, PyFrameObject *py_frame, int what, PyObject *arg)

cdef extern from "Python.h":
    ctypedef struct PyCodeObject
    ctypedef long long PY_LONG_LONG
    cdef bint PyCFunction_Check(object obj)

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

cdef extern from "code.h":
    cdef int PyCode_Addr2Line(PyCodeObject *co, int byte_offset)

cdef extern from "timers.c":
    PY_LONG_LONG hpTimer()
    double hpTimerUnit()

cdef extern from "unset_trace.c":
    void unset_trace()

cdef struct LineTime:
    # long long int is at least 64 bites assuming c99
    int64 code
    int lineno
    PY_LONG_LONG total_time
    long nhits
    
cdef struct LastTime:
    PY_LONG_LONG time
    int f_lineno

def label(code):
    """ Return a (filename, first_lineno, func_name) tuple for a given code
    object.

    This is the same labelling as used by the cProfile module in Python 2.5.
    """
    if isinstance(code, str):
        return ('~', 0, code)    # built-in functions ('~' sorts at the end)
    else:
        return (code.co_filename, code.co_firstlineno, code.co_name)

# Note: this is a regular Python class to allow easy pickling.
class LineStats(object):
    """ Object to encapsulate line-profile statistics.

    Attributes
    ----------
    timings : dict
        Mapping from (filename, first_lineno, function_name) of the profiled
        function to a list of (lineno, nhits, total_time) tuples for each
        profiled line. total_time is an integer in the native units of the
        timer.
    unit : float
        The number of seconds per timer unit.
    """
    def __init__(self, timings, unit):
        self.timings = timings
        self.unit = unit

cdef class LineProfiler:
    """ Time the execution of lines of Python code.
    """
    cdef unordered_map[int64, unordered_map[int64, LineTime]] c_code_map
    cdef unordered_map[int64, LastTime] c_last_time
    cdef public list functions
    cdef public dict code_hash_map
    cdef public double timer_unit
    cdef public long enable_count

    def __init__(self, *functions):
        self.functions = []
        self.code_hash_map = {}
        self.timer_unit = hpTimerUnit()
        self.enable_count = 0
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
            import warnings
            warnings.warn("Could not extract a code object for the object %r" % (func,))
            return

        # TODO: Since each line can be many bytecodes, this is kinda inefficient
        # See if this can be sped up by not needing to iterate over every byte
        for offset, byte in enumerate(code.co_code):
            code_hash = hash((code.co_code)) ^ PyCode_Addr2Line(<PyCodeObject*>code, offset)
            
            if not self.c_code_map.count(code_hash):
                try:
                    self.code_hash_map[code].append(code_hash)
                except KeyError:
                    self.code_hash_map[code] = [code_hash]
                self.c_code_map[code_hash]
            self.functions.append(func)

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

    def enable(self):
        PyEval_SetTrace(python_trace_callback, self)
        
    @property
    def code_map(self):
        return <dict>self.c_code_map
        
    @property
    def last_time(self):
        return <dict>self.c_last_time

    cpdef disable(self):
        self.c_last_time.clear()
        unset_trace()

    cpdef get_stats(self):
        """ Return a LineStats object containing the timings.
        """
        cdef dict cmap
        
        stats = {}
        for code in self.code_hash_map:
            cmap = self.c_code_map
            entries = []
            for entry in self.code_hash_map[code]:
                entries += list(cmap[entry].values())
            key = label(code)
            stats[key] = [(e["lineno"], e["nhits"], e["total_time"]) for e in entries]
            stats[key].sort()
        return LineStats(stats, self.timer_unit)

@cython.boundscheck(False)
@cython.wraparound(False)
cdef int python_trace_callback(object self_, PyFrameObject *py_frame, int what,
    PyObject *arg):
    """ The PyEval_SetTrace() callback.
    """
    cdef LineProfiler self
    cdef object code
    cdef LineTime entry
    cdef LastTime old
    cdef int key
    cdef PY_LONG_LONG time
    cdef int64 code_hash
    cdef uint64 block_hash
    cdef unordered_map[int64, LineTime] line_entries

    self = <LineProfiler>self_

    if what == PyTrace_LINE or what == PyTrace_RETURN:
        block_hash = hash((<object>py_frame.f_code.co_code))
        code_hash = block_hash ^ py_frame.f_lineno
        if self.c_code_map.count(code_hash):
            time = hpTimer()
            if self.c_last_time.count(block_hash):
                old = self.c_last_time[block_hash]
                line_entries = self.c_code_map[code_hash]
                key = old.f_lineno
                if not line_entries.count(key):
                    self.c_code_map[code_hash][key] = LineTime(code_hash, key, 0, 0)
                self.c_code_map[code_hash][key].nhits += 1
                self.c_code_map[code_hash][key].total_time += time - old.time
            if what == PyTrace_LINE:
                # Get the time again. This way, we don't record much time wasted
                # in this function.
                self.c_last_time[block_hash] = LastTime(hpTimer(), py_frame.f_lineno)
            else:
                # We are returning from a function, not executing a line. Delete
                # the last_time record. It may have already been deleted if we
                # are profiling a generator that is being pumped past its end.
                if self.c_last_time.count(block_hash):
                    self.c_last_time.erase(self.c_last_time.find(block_hash))

    return 0


