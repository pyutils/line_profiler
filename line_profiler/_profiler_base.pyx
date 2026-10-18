# cython: language_level=3
# cython: infer_types=True
# cython: legacy_implicit_noexcept=True
# distutils: language=c++
# distutils: include_dirs = python25.pxd
r"""
The profiling data and the (non-tracing) bookkeeping of
line_profiler._line_profiler.LineProfiler.
"""
from sys import byteorder
import sys
from cpython.version cimport PY_VERSION_HEX
import threading
import opcode
import os
import types
from warnings import warn


NOP_VALUE: int = opcode.opmap['NOP']

# The Op code should be 2 bytes as stated in
# https://docs.python.org/3/library/dis.html
# if sys.version_info[0:2] >= (3, 11):
NOP_BYTES_LEN: int = 2
NOP_BYTES: bytes = NOP_VALUE.to_bytes(NOP_BYTES_LEN, byteorder=byteorder)

# This should be true for Python >=3.11a1
HAS_CO_QUALNAME: bool = hasattr(types.CodeType, 'co_qualname')

# Can't line-profile Cython in 3.12 since the old C API was upended
# without an appropriate replacement (which only came in 3.13);
# see also:
# https://cython.readthedocs.io/en/latest/src/tutorial/profiling_tutorial.html
_CAN_USE_SYS_MONITORING = PY_VERSION_HEX >= 0x030c00b1
CANNOT_LINE_TRACE_CYTHON = (
    _CAN_USE_SYS_MONITORING and PY_VERSION_HEX < 0x030d00b1)

cdef extern from "Python_wrapper.h":
    ctypedef struct PyCodeObject

    cdef int PyCode_Addr2Line(PyCodeObject *co, int byte_offset)

    cdef unsigned long PyThread_get_thread_ident()


# Whether the sys.monitoring callbacks have returned
# sys.monitoring.DISABLE for any code location since the last
# sys.monitoring.restart_events()
cdef bint _events_disabled = False


cdef void note_events_disabled() noexcept:
    """
    Record that a sys.monitoring callback has disabled the event at
    a code location (because it isn't being profiled).
    """
    global _events_disabled
    _events_disabled = True


cdef void restart_disabled_events():
    """
    Re-enable the sys.monitoring events disabled at code locations,
    if any.  This is needed when the set of profiled code changes, since
    code which we disabled the events for may now need to be profiled.

    Note:
        sys.monitoring.restart_events() re-enables disabled
        events for all tools, but tools are supposed to cope with that
        (they just disable the events again).
    """
    global _events_disabled
    if _events_disabled:
        _events_disabled = False
        sys.monitoring.restart_events()


cdef inline object multibyte_rstrip(bytes bytecode):
    """
    Returns:
        result (tuple[bytes, int])
        - First item is the bare unpadded bytecode
        - Second item is the number of :py:data:`NOP_BYTES`
          ``bytecode`` has been padded with
    """
    npad: int = 0
    nop_len: int = -NOP_BYTES_LEN
    nop_bytes: bytes = NOP_BYTES
    unpadded: bytes = bytecode
    while unpadded.endswith(nop_bytes):
        unpadded = unpadded[:nop_len]
        npad += 1
    return (unpadded, npad)


def label(code):
    """
    Return a ``(filename, first_lineno, _name)`` tuple for a given code
    object.

    This is the similar labelling as used by the :py:mod:`cProfile`
    module in Python 2.5.

    Note:
        In Python >= 3.11 we return qualname for ``_name``.
        In older versions of Python we just return name.
    """
    if isinstance(code, str):
        return ('~', 0, code)  # built-in functions ('~' sorts at the end)
    else:
        if HAS_CO_QUALNAME:
            return (code.co_filename, code.co_firstlineno, code.co_qualname)
        else:
            return (code.co_filename, code.co_firstlineno, code.co_name)


def find_cython_source_file(cython_func):
    """
    Resolve the absolute path to a Cython function's source file.

    Returns:
        result (str | None)
            Cython source file if found, else :py:data:`None`.
    """
    try:
        compiled_module = cython_func.__globals__['__file__']
    except KeyError:  # Shouldn't happen...
        return None
    rel_source_file = cython_func.__code__.co_filename
    if os.path.isabs(rel_source_file):
        if os.path.isfile(rel_source_file):
            return rel_source_file
        return None
    prefix = os.path.dirname(compiled_module)
    while True:
        source_file = os.path.join(prefix, rel_source_file)
        if os.path.isfile(source_file):
            return source_file
        next_prefix = os.path.dirname(prefix)
        if next_prefix == prefix:  # At the file-system root
            return None
        prefix = next_prefix


cpdef _code_replace(func, co_code):
    """
    Implements :py:mod:`~code.replace` for Python < 3.8
    """
    try:
        code = func.__code__
    except AttributeError:
        code = func.__func__.__code__
    if hasattr(code, 'replace'):
        # python 3.8+
        code = _copy_local_sysmon_events(code, code.replace(co_code=co_code))
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


cpdef _copy_local_sysmon_events(old_code, new_code):
    """
    Copy the local events from ``old_code`` over to ``new_code`` where
    appropriate.

    Returns:
        code: ``new_code``
    """
    try:
        mon = sys.monitoring
    except AttributeError:  # Python < 3.12
        return new_code
    # Tool ids are integers in the range 0 to 5 inclusive.
    # https://docs.python.org/3/library/sys.monitoring.html#tool-identifiers
    NUM_TOOLS = 6  
    for tool_id in range(NUM_TOOLS):
        try:
            events = mon.get_local_events(tool_id, old_code)
            mon.set_local_events(tool_id, new_code, events)
        except ValueError:  # Tool ID not in use
            pass
    return new_code


cdef class _LineProfilerBase:
    """
    Base class of line_profiler._line_profiler.LineProfiler
    holding the profiling data, and the methods for setting up and
    inspecting it.

    Note:
        This is split from line_profiler._line_profiler so
        that the extension modules can be compiled in parallel; the
        trace callbacks there access the C-level data directly.
    """
    # type: ClassVar[dict[bytes, int]], bytes = bytecode
    _all_paddings = {}

    cpdef add_function(self, func):
        """
        Record line profiling information for the given Python function.

        Note:
            This is a low-level method and is intended for |function|_;
            users should in general use
            :py:meth:`line_profiler.LineProfiler.add_callable` for
            adding general callables and callable wrappers (e.g.
            :py:class:`property`).

        .. |function| replace:: :py:class:`types.FunctionType`
        .. _function: https://docs.python.org/3/reference/\
datamodel.html#user-defined-functions
        """
        if hasattr(func, "__wrapped__"):
            warn(
                "Adding a function with a `.__wrapped__` attribute. "
                "You may want to profile the wrapped function by adding "
                f"`{func.__name__}.__wrapped__` instead."
            )
        try:
            code = func.__code__
            func_id = id(func)
        except AttributeError:
            try:
                code = func.__func__.__code__
                func_id = id(func.__func__)
            except AttributeError:
                warn(
                    f"Could not extract a code object for the object {func!r}")
                return

        # Note: if we are to alter the code object, other profilers
        # which previously added this function would still expect the
        # old bytecode, and thus will not see anything when the function
        # is executed;
        # hence:
        # - When doing bytecode padding, take into account all instances
        #   which refers to the same base bytecode to ensure
        #   disambiguation
        # - Update all existing instances referring to the old code
        #   object
        # Since no code padding is/can be done with Cython mock
        # "code objects", it is *probably* okay to only do the special
        # handling on the non-Cython branch.
        # XXX: tests for the above assertion if necessary
        co_code: bytes = code.co_code
        code_hashes = []
        if any(co_code):  # Normal Python functions
            # Figure out how much padding we need and strip the bytecode
            # Notes:
            # - Profiled function are always padded, so as to
            #   distinguish between them and unprofiled bytecode twins
            # - `npad` is strictly increasing, except when the function
            #   has already been padded -- we assume that is solely
            #   because a (could be the same) profiler instance has seen
            #   it
            base_co_code: bytes
            npad: int
            is_padded: int
            base_co_code, is_padded = multibyte_rstrip(co_code)
            if not is_padded:
                try:
                    npad = self._all_paddings[base_co_code]
                except KeyError:
                    npad = 1
                self._all_paddings[base_co_code] = npad + 1
                # Code hash already exists, so there must be a duplicate
                # function (on some instance);
                # (re-)pad with no-op
                co_code = base_co_code + NOP_BYTES * npad
                code = _code_replace(func, co_code)
                try:
                    func.__code__ = code
                except AttributeError as e:
                    func.__func__.__code__ = code
            # Maintain `.dupes_map` (legacy)
            try:
                self.dupes_map[base_co_code].append(code)
            except KeyError:
                self.dupes_map[base_co_code] = [code]
            # TODO: Since each line can be many bytecodes, this is kinda
            # inefficient
            # See if this can be sped up by not needing to iterate over
            # every byte
            for offset, _ in enumerate(co_code):
                code_hashes.append(
                    compute_line_hash(
                        hash(co_code),
                        PyCode_Addr2Line(<PyCodeObject*>code, offset)))
        else:  # Cython functions have empty/zero bytecodes
            if CANNOT_LINE_TRACE_CYTHON:
                return

            from line_profiler.line_profiler import get_code_block

            lineno = code.co_firstlineno
            if hasattr(func, '__code__'):
                cython_func = func
            else:
                cython_func = func.__func__
            cython_source = find_cython_source_file(cython_func)
            if not cython_source:  # Can't find the source
                return
            nlines = len(get_code_block(cython_source, lineno))
            block_hash = hash(code)
            for lineno in range(lineno, lineno + nlines):
                code_hash = compute_line_hash(block_hash, lineno)
                code_hashes.append(code_hash)
            # We can't replace the code object on Cython functions, but
            # we can *store* a copy with the correct metadata
            # Note: we don't use `_copy_local_sysmon_events()` here
            # because Cython shim code objects don't support local
            # events
            code = code.replace(co_filename=cython_source)
        # Update `._c_code_map` and `.code_hash_map` with the new line
        # hashes
        try:
            line_hashes = self.code_hash_map[code]
        except KeyError:
            line_hashes = self.code_hash_map[code] = []
        for code_hash in code_hashes:
            line_hash = <int64>code_hash
            if not self._c_code_map.count(line_hash):
                line_hashes.append(line_hash)
                self._c_code_map[line_hash]

        self.functions.append(func)
        # The events of the code may have been disabled while it wasn't
        # being profiled
        restart_disabled_events()

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
        """
        Disable the profiler if the number of disable requests matches
        (or exceeds) the number of enable requests.
        """
        if self.enable_count > 0:
            self.enable_count -= 1
            if self.enable_count == 0:
                self.disable()

    def __enter__(self):
        self.enable_by_count()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disable_by_count()

    @property
    def c_code_map(self):
        """
        A Python view of the internal C lookup table.
        """
        return <dict>self._c_code_map

    @property
    def c_last_time(self):
        """
        Raises:
            KeyError: If no profiling data is available on the current thread.

        Note:
            Internally the records are kept per frame (so that recursive
            calls don't overwrite each other's), but this view is keyed
            by block hash as before; if a function has more than one
            active frame, the record of the innermost one is used.
        """
        try:
            records = (<dict>self._c_last_time)[PyThread_get_thread_ident()]
        except KeyError as e:
            # We haven't actually profiled anything yet
            raise (KeyError('No profiling data on the current thread '
                            '(`threading.get_ident()` = '
                            f'{threading.get_ident()})')
                   .with_traceback(e.__traceback__)) from None
        c_last_time = {}
        for record in records.values():
            block_hash = record.pop('block_hash')
            current = c_last_time.get(block_hash)
            if current is None or record['time'] > current['time']:
                c_last_time[block_hash] = record
        return c_last_time

    @property
    def code_map(self):
        """
        :py:mod:`line_profiler` 4.0 no longer directly maintains
        :py:attr:`~.code_map`, but this will construct something similar
        for backwards compatibility.
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
        :py:mod:`line_profiler` 4.0 no longer directly maintains
        :py:attr:`~.last_time`, but this will construct something similar
        for backwards compatibility.
        """
        c_last_time = self.c_last_time
        py_last_time = {}
        for code in self.code_hash_map:
            block_hash = hash(code.co_code)
            if block_hash in c_last_time:
                py_last_time[code] = c_last_time[block_hash]
        return py_last_time
