#cython: language_level=3
from .python25 cimport PyFrameObject, PyObject, PyStringObject
from .phashmap cimport flat_hash_map, parallel_flat_hash_map, parallel_flat_hash_set
from preshed.maps cimport PreshMap
from sys import byteorder
cimport cython
from cpython.object cimport PyObject_Hash
from cpython.version cimport PY_VERSION_HEX
from libc.stdint cimport int64_t

from libcpp.cast cimport reinterpret_cast
from libcpp.unordered_map cimport unordered_map
from libcpp.utility cimport pair
from threading import get_ident, local

# long long int is at least 64 bytes assuming c99
ctypedef unsigned long long int uint64
ctypedef long long int int64

cdef extern from "pythread.h":
    cdef int PyThread_tss_is_created(Py_tss_t *key)
    cdef int PyThread_tss_create(Py_tss_t *key)
    cdef int PyThread_tss_set(Py_tss_t *key, void *value)
    cdef void* PyThread_tss_get(Py_tss_t *key)
    cdef struct Py_tss_t

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
    cdef object get_frame_code(PyFrameObject* frame) noexcept
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

cdef extern from "timers.h":
    PY_LONG_LONG hpTimer() noexcept
    double hpTimerUnit() noexcept

cdef extern from "unset_trace.h":
    void unset_trace() noexcept

cdef struct LineTime:
    int64 code
    int lineno
    PY_LONG_LONG total_time
    long nhits
    
cdef struct LastTime:
    int f_lineno
    PY_LONG_LONG time

cdef inline int64 compute_line_hash(uint64 block_hash, uint64 linenum) noexcept:
    """
    Compute the hash used to store each line timing in an unordered_map.
    This is fairly simple, and could use some improvement since linenum
    isn't technically random, however it seems to be good enough and
    fast enough for any practical purposes.
    """
    # linenum doesn't need to be int64 but it's really a temporary value
    # so it doesn't matter
    return block_hash ^ linenum

def label(code):
    """ Return a (filename, first_lineno, func_name) tuple for a given code
    object.

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
    """ 
    Time the execution of lines of Python code.

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
    """
    cdef parallel_flat_hash_map[int64, flat_hash_map[int64, LineTime]] _c_code_map
    cdef parallel_flat_hash_map[int64, flat_hash_map[int64, LastTime]] _c_last_time
    # sadly we can't put a preshmap inside a preshmap
    # so we can only use it to speed up top-level lookups
    cdef PreshMap _c_code_map_set, _c_thread_ids
    cdef public list functions
    cdef public dict code_hash_map, dupes_map
    cdef public double timer_unit
    cdef public object threaddata, get_ident

    def __init__(self, *functions):
        self.functions = []
        self.code_hash_map = {}
        self.dupes_map = {}
        self.timer_unit = hpTimerUnit()
        self.threaddata = local()
        # bind it to local class for faster lookups
        self.get_ident = get_ident
        self._c_thread_ids = PreshMap(initial_size=2)
        # use these for quick membership tests in the callback
        self._c_code_map_set = PreshMap(256)

        for func in functions:
            self.add_function(func)

    cpdef add_function(self, func):
        cdef uint64 sentinel = 2
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
            co_code = code.co_code + (9).to_bytes(1, byteorder=byteorder) * (len(self.dupes_map[code.co_code]))
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
            if not <uint64>code_hash in self._c_code_map_set:
                try:
                    self.code_hash_map[code].append(code_hash)
                except KeyError:
                    self.code_hash_map[code] = [code_hash]
                self._c_code_map[code_hash]
                # can hold 128 lines per function before rehashing
                self._c_code_map[code_hash].reserve(128)
                self._c_code_map_set.set(<uint64>code_hash, &sentinel)

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

    def enable(self):
        PyEval_SetTrace(python_trace_callback, self)
        
    @property
    def c_code_map(self):
        """
        A Python view of the internal C lookup table.
        """
        return self.convert_pmap_line()
        
    @property
    def c_last_time(self):
        return self.convert_pmap_last()[self.get_ident()]

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
        c_last_time = self.convert_pmap_last()[self.get_ident()]
        code_hash_map = self.code_hash_map
        py_last_time = {}
        for code, code_hashes in code_hash_map.items():
            for code_hash in code_hashes:
                if code_hash in c_last_time:
                    py_last_time[code] = c_last_time[code_hash]
        return py_last_time


    cpdef disable(self):
        self._c_last_time[self.get_ident()].clear()
        unset_trace()

    def get_stats(self):
        """ Return a LineStats object containing the timings.
        """
        cdef dict cmap
        
        stats = {}
        for code in self.code_hash_map:
            cmap = self.convert_pmap_line()
            entries = []
            for entry in self.code_hash_map[code]:
                entries += list(cmap[entry].values())
            key = label(code)

            entries = [(e["lineno"], e["nhits"], e["total_time"]) for e in entries]
            # If there are multiple entries for a line, this will sort them by increasing # hits
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
    
    # We make two separate functions because fused templates are an even bigger mess without using python
    @cython.boundscheck(False)
    @cython.wraparound(False)
    cdef dict convert_pmap_last(self_):
        cdef LineProfiler self
        cdef unordered_map[int64, unordered_map[int64, LastTime]] temp
        cdef unordered_map[int64, LastTime] temp2
        cdef pair[int64, flat_hash_map[int64, LastTime]] kv
        cdef pair[int64, LastTime] kv2
        
        self = <LineProfiler>self_

        for kv in self._c_last_time:
            temp[kv.first]
            for kv2 in kv.second:
                temp2[kv2.first] = kv2.second
                temp[kv.first] = temp2
        return <dict>temp

    @cython.boundscheck(False)
    @cython.wraparound(False)
    cdef dict convert_pmap_line(self_):
        cdef LineProfiler self
        cdef unordered_map[int64, unordered_map[int64, LineTime]] temp
        cdef unordered_map[int64, LineTime] temp2
        cdef pair[int64, flat_hash_map[int64, LineTime]] kv
        cdef pair[int64, LineTime] kv2
        
        self = <LineProfiler>self_

        for kv in self._c_code_map:
            temp[kv.first]
            for kv2 in kv.second:
                temp2[kv2.first] = kv2.second
                temp[kv.first] = temp2
        return <dict>temp


@cython.boundscheck(False)
@cython.wraparound(False)
cdef int python_trace_callback(object self_, PyFrameObject *py_frame, int what,
PyObject *arg) noexcept:
    """ The PyEval_SetTrace() callback.
    """
    cdef LineProfiler self
    cdef object code
    cdef LineTime entry
    cdef LastTime old
    cdef int key
    cdef PY_LONG_LONG time
    cdef int64 code_hash, block_hash
    cdef flat_hash_map[int64, LineTime] *line_entries
    cdef flat_hash_map[int64, LastTime] *last_entries
    cdef Py_tss_t tss_key
    cdef int *temp
    # empty key is 0 and deleted key is 1
    cdef uint64 sentinel = 2, ident

    self = <LineProfiler>self_

    if what == PyTrace_LINE or what == PyTrace_RETURN:
        # Normally we'd need to DECREF the return from get_frame_code, but Cython does that for us
        block_hash = PyObject_Hash(get_frame_code(py_frame))
        code_hash = compute_line_hash(block_hash, py_frame.f_lineno)
        # we have to use reinterpret_cast because get returns void*
        if reinterpret_cast[uint64](self._c_code_map_set.get(<uint64>code_hash)):
            time = hpTimer()
            ident = reinterpret_cast[uint64](PyThread_tss_get(&tss_key))
            if reinterpret_cast[uint64](self._c_thread_ids.get(ident)):
                PyThread_tss_set(&tss_key, &ident)
                self._c_thread_ids.set(ident, &sentinel)
                # we have a new tss value -- redo ident
                ident = reinterpret_cast[uint64](PyThread_tss_get(&tss_key))
                # allocate space on the first time, to prevent excessive rehashing
                self._c_last_time[ident].reserve(256)
            last_entries = &(self._c_last_time[ident])
            # tried replacing the remaining instances of map.count() with a preshed map
            # but that made it slightly slower somehow
            # use [0] to get the value from the pointer
            if last_entries[0].count(block_hash):
                # this block handles the case where a line has been seen before
                line_entries = &self._c_code_map[code_hash]
                old = last_entries[0][block_hash]
                key = old.f_lineno
                # count() has been profiled and is faster than .find() and a null pointer comparison
                if not line_entries[0].count(key):
                    line_entries[0][key] = LineTime(code_hash, key, 0, 0)
                    self._c_code_map_set.set(<uint64>code_hash, &ident)
                line_entries[0][key].nhits += 1
                line_entries[0][key].total_time += time - old.time
            if what == PyTrace_LINE:
                # Initialize the last_entries entry for each line
                # This callback is now fast enough that calculating hpTimer() again
                # would slow down the callback in the worst-case from 9x overhead to 12x
                # and on average, 1.5x to 1.8x overhead. Luckily, there is no
                # measureable timing inaccuracy from reusing the old time value
                last_entries[0][block_hash] = LastTime(py_frame.f_lineno, time)
            elif last_entries[0].count(block_hash):
                # We are returning from a function, not executing a line. Delete
                # the last_time record. It may have already been deleted if we
                # are profiling a generator that is being pumped past its end.
                last_entries[0].erase(last_entries[0].find(block_hash))

    return 0


