# cython: language_level=3
# cython: legacy_implicit_noexcept=True
# used in _line_profiler.pyx
from libcpp.unordered_map cimport unordered_map

from ._map_helpers cimport LastTimeMap, LineTimeMap

# long long int is at least 64 bytes assuming c99
ctypedef unsigned long long int uint64
ctypedef long long int int64


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


# Bookkeeping for the sys.monitoring.DISABLE-ing of events at code
# locations which aren't being profiled (see _line_profiler.pyx)
cdef void note_events_disabled() noexcept
cdef void restart_disabled_events()


cdef class _LineProfilerBase:
    cdef unordered_map[int64, LineTimeMap] _c_code_map
    # Mapping between thread-id and map of LastTime
    cdef unordered_map[int64, LastTimeMap] _c_last_time
    cdef public list functions
    cdef public dict code_hash_map, dupes_map
    cdef public double timer_unit
    cdef public object threaddata

    cpdef add_function(self, func)
