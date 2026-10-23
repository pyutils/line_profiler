# cython: language_level=3
# Declarations for _profile_data.hpp; used in _line_profiler.pyx and
# _profiler_base.pyx
from libcpp cimport bool as cbool
from libcpp.vector cimport vector

cdef extern from "Python_wrapper.h":
    ctypedef struct PyObject
    ctypedef Py_ssize_t Py_hash_t
    ctypedef long long PY_LONG_LONG

cdef extern from "_profile_data.hpp" namespace "line_profiler":
    cdef cppclass LineData:
        long nhits
        PY_LONG_LONG total_time
        cbool registered

    cdef cppclass LastLine:
        unsigned long thread_ident
        const void *frame_key
        int line_index
        PY_LONG_LONG time

    cdef cppclass ProfileEntry:
        PyObject *profiler
        Py_hash_t block_hash
        int first_line
        vector[LineData] lines
        vector[LastLine] last_lines

        ProfileEntry(PyObject *profiler, Py_hash_t block_hash)
        cbool register_line(int lineno)
        int line_index(int lineno)
        LastLine *find_last_line(unsigned long thread_ident,
                                 const void *frame_key)
        LastLine *get_last_line(unsigned long thread_ident,
                                const void *frame_key)
        cbool has_thread(unsigned long thread_ident)
        const LastLine *latest_last_line(unsigned long thread_ident)
        void clear_last_lines(unsigned long thread_ident)

    cdef cppclass Block:
        Py_hash_t block_hash
        vector[ProfileEntry *] entries

        Block(Py_hash_t block_hash)

    cdef cppclass CodeCache:
        unsigned long generation
        Py_hash_t block_hash
        Block *block

    void free_code_cache(void *cache)
    CodeCache *new_code_cache(Py_hash_t block_hash)
