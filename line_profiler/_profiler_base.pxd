# cython: language_level=3
# cython: legacy_implicit_noexcept=True
# used in _line_profiler.pyx
from libcpp.vector cimport vector

from ._profile_data cimport Py_hash_t, ProfileEntry

# long long int is at least 64 bytes assuming c99
ctypedef unsigned long long int uint64
ctypedef long long int int64


cdef inline int64 compute_line_hash(uint64 block_hash, uint64 linenum) noexcept:
    """
    Compute the hash identifying each line of a code block, as found in
    :py:attr:`LineProfiler.code_hash_map` and
    :py:attr:`LineProfiler.c_code_map`.
    This is fairly simple, and could use some improvement since linenum
    isn't technically random, however it seems to be good enough and
    fast enough for any practical purposes.
    """
    # linenum doesn't need to be int64 but it's really a temporary value
    # so it doesn't matter
    return block_hash ^ linenum


# Bookkeeping for the sys.monitoring.DISABLE-ing of events at code
# locations which aren't being profiled (see _line_profiler.pyx)
cdef void note_events_disabled() noexcept
cdef void restart_disabled_events() noexcept


cdef class _LineProfilerBase:
    # The profiling data of each code block (owned by the instance; also
    # referenced by the blocks in the registry in _line_profiler.pyx)
    cdef vector[ProfileEntry *] _entries
    # Idents of the threads on which the profiler has been disabled (see
    # .c_last_time)
    cdef set _disabled_threads
    # Factor converting the raw timings to .timer_units
    cdef double _ticks_to_units
    cdef public list functions
    cdef public dict code_hash_map, dupes_map
    cdef public double timer_unit
    cdef public object threaddata

    cpdef add_function(self, func)
    cdef list _register_lines(self, Py_hash_t block_hash, object linenos)
