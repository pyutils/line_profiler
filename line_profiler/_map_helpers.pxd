# cython: language_level=3
# cython: legacy_implicit_noexcept=True
# used in _line_profiler.pyx
from libcpp.unordered_map cimport unordered_map

# long long int is at least 64 bytes assuming c99
ctypedef long long int int64

cdef extern from "Python_wrapper.h":
    ctypedef long long PY_LONG_LONG

cdef struct LastTime:
    int f_lineno
    PY_LONG_LONG time
    long long block_hash

cdef struct LineTime:
    long long code
    int lineno
    PY_LONG_LONG total_time
    long nhits

# Types used for mappings to last/line times; LastTimeMap is keyed by frame
# address (Python code) or code-block hash (Cython code under
# sys.monitoring), LineTimeMap by line hash.
ctypedef unordered_map[int64, LastTime] LastTimeMap
ctypedef unordered_map[int64, LineTime] LineTimeMap
