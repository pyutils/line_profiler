# cython: language_level=3
# cython: legacy_implicit_noexcept=True
# used in _line_profiler.pyx
from libcpp.unordered_map cimport unordered_map
from cython.operator cimport dereference as deref

# long long int is at least 64 bytes assuming c99
ctypedef long long int int64

cdef extern from "Python_wrapper.h":
    ctypedef long long PY_LONG_LONG

cdef struct LastTime:
    int f_lineno
    PY_LONG_LONG time

cdef struct LineTime:
    long long code
    int lineno
    PY_LONG_LONG total_time
    long nhits

# Types used for mappings from code hash to last/line times.
ctypedef unordered_map[int64, LastTime] LastTimeMap
ctypedef unordered_map[int64, LineTime] LineTimeMap

cdef inline void last_erase_if_present(LastTimeMap* m, int64 key) noexcept:
    cdef LastTimeMap.iterator it = deref(m).find(key)
    if it != deref(m).end():
        deref(m).erase(it)

cdef inline LineTime* line_ensure_entry(LineTimeMap* m, int lineno, long long code_hash) noexcept:
    if not deref(m).count(lineno):
        deref(m)[lineno] = LineTime(code_hash, lineno, 0, 0)
    return &(deref(m)[lineno])
