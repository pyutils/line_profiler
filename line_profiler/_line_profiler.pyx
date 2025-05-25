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
import functools
import threading
import opcode
import os
import types
from weakref import WeakSet

NOP_VALUE: int = opcode.opmap['NOP']

# The Op code should be 2 bytes as stated in
# https://docs.python.org/3/library/dis.html
# if sys.version_info[0:2] >= (3, 11):
NOP_BYTES_LEN: int = 2
NOP_BYTES: bytes = NOP_VALUE.to_bytes(NOP_BYTES_LEN, byteorder=byteorder)

# This should be true for Python >=3.11a1
HAS_CO_QUALNAME: bool = hasattr(types.CodeType, 'co_qualname')

# "Lightweight" monitoring in 3.12.0b1+
CAN_USE_SYS_MONITORING = PY_VERSION_HEX >= 0x030c00b1

# Can't line-trace Cython in 3.12
# (TODO: write monitoring hook, Cython function line events are emitted
# only via `sys.monitoring` and are invisible to the "legacy" tracing
# system)
CANNOT_LINE_TRACE_CYTHON = (
    CAN_USE_SYS_MONITORING and PY_VERSION_HEX < 0x030d00b1)

# long long int is at least 64 bytes assuming c99
ctypedef unsigned long long int uint64
ctypedef long long int int64

cdef extern from "Python_wrapper.h":
    """
    #if PY_VERSION_HEX < 0x030900b1  // 3.9.0b1
        /*
         * Notes:
         *     While 3.9.0a1 already has `PyFrame_GetCode()`, it doesn't
         *     INCREF the code object until 0b1 (PR #19773), so override
         *     that for consistency.
         */
        inline PyCodeObject *get_frame_code(
            PyFrameObject *frame
        ) {
            PyCodeObject *code;
            assert(frame != NULL);
            code = frame->f_code;
            assert(code != NULL);
            Py_INCREF(code);
            return code;
        }
    #else
        #define get_frame_code(x) PyFrame_GetCode(x)
    #endif
    """
    ctypedef struct PyCodeObject
    cdef PyCodeObject* get_frame_code(PyFrameObject* frame)
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

cdef extern from "c_trace_callbacks.c":
    ctypedef struct TraceCallback:
        Py_tracefunc c_tracefunc
        PyObject *c_traceobj

    cdef TraceCallback *alloc_callback() except *
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


cdef inline object multibyte_rstrip(bytes bytecode):
    """
    Returns:
        result (tuple[bytes, int])
        - First item is the bare unpadded bytecode
        - Second item is the number of :py:const:`NOP_BYTES`
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


if CAN_USE_SYS_MONITORING:
    def _is_main_thread() -> bool:
        return threading.current_thread() == threading.main_thread()


def label(code):
    """
    Return a ``(filename, first_lineno, _name)`` tuple for a given code
    object.

    This is the similar labelling as used by the :py:mod:`cProfile`
    module in Python 2.5.

    Note:
        In Python >= 3.11 we use we return qualname for ``_name``.
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
            Cython source file if found, else :py:const:`None`.
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


def disable_line_events(trace_func: Callable) -> Callable:
    """
    Return a thin wrapper around ``trace_func()`` which withholds line
    events.  This is for when a frame-local
    :py:attr:`~types.FrameType.f_trace` disables
    :py:attr:`~types.FrameType.f_trace_lines` -- we would like to keep
    line events enabled (so that line profiling works) while
    "unsubscribing" the trace function from it.
    """
    @wraps(trace_func)
    def wrapper(frame, event, args):
        if event == 'line':
            return
        return trace_func(frame, event, args)

    return wrapper


cpdef _code_replace(func, co_code):
    """
    Implements :py:mod:`types.CodeType.replace` for Python < 3.8
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

        timings (dict[tuple[str, int, str], \
list[tuple[int, int, int]]]):
            Mapping from ``(filename, first_lineno, function_name)`` of
            the profiled function to a list of
            ``(lineno, nhits, total_time)`` tuples for each profiled
            line. ``total_time`` is an integer in the native units of
            the timer.

        unit (float):
            The number of seconds per timer unit.
    """
    def __init__(self, timings, unit):
        self.timings = timings
        self.unit = unit


cdef class ThreadState:
    """
    Helper object for holding the thread-local state; documentations are
    for reference only, and all APIs are to be considered private and
    subject to change.
    """
    cdef TraceCallback *legacy_callback
    cdef dict mon_callbacks  # type: dict[int, Callable | None]
    cdef public object active_instances  # type: set[LineProfiler]
    cdef public int _wrap_trace

    def __init__(self, instances=(), wrap_trace=False):
        self.active_instances = set(instances)
        self.legacy_callback = NULL
        self.mon_callbacks = {}
        self.wrap_trace = wrap_trace

    cpdef handle_line_event(self, object code, int lineno):
        """
        Line-event (`sys.monitoring.events.LINE`) callback passed to
        :py:func:`sys.monitoring.register_callback`.
        """
        inner_trace_callback(1, self.active_instances, code, lineno)
        if self.wrap_trace:  # Call wrapped callback
            callback = self.mon_callbacks.get(sys.monitoring.events.LINE)
            if callback is not None:
                callback(code, lineno)

    cpdef handle_return_event(
            self, object code, int instruction_offset, object retval):
        """
        Return-event (`sys.monitoring.events.PY_RETURN`) callback passed
        to :py:func:`sys.monitoring.register_callback`.
        """
        self._handle_exit_event(
            sys.monitoring.events.PY_RETURN, code, instruction_offset, retval)

    cpdef handle_yield_event(
            self, object code, int instruction_offset, object retval):
        """
        Yield-event (`sys.monitoring.events.PY_YIELD`) callback passed
        to :py:func:`sys.monitoring.register_callback`.
        """
        self._handle_exit_event(
            sys.monitoring.events.PY_YIELD, code, instruction_offset, retval)

    cpdef handle_raise_event(
            self, object code, int instruction_offset, object exception):
        """
        Raise-event (`sys.monitoring.events.RAISE`) callback passed
        to :py:func:`sys.monitoring.register_callback`.
        """
        self._handle_exit_event(
            sys.monitoring.events.RAISE, code, instruction_offset, exception)

    cpdef handle_reraise_event(
            self, object code, int instruction_offset, object exception):
        """
        Reraise-event (`sys.monitoring.events.RERAISE`) callback passed
        to :py:func:`sys.monitoring.register_callback`.
        """
        self._handle_exit_event(
            sys.monitoring.events.RERAISE, code, instruction_offset, exception)

    cpdef _handle_exit_event(
            self, int event_id, object code, int offset, object obj):
        """
        Base for the frame-exit-event (e.g. via returning or yielding)
        callback passed to :py:func:`sys.monitoring.register_callback`.
        """
        cdef int lineno = PyCode_Addr2Line(
            <PyCodeObject*>code, offset)
        inner_trace_callback(0, self.active_instances, code, lineno)
        if self.wrap_trace:  # Call wrapped callback
            callback = self.mon_callbacks.get(event_id)
            if callback is not None:
                callback(code, offset, obj)

    cpdef _handle_enable_event(self, prof):
        cdef TraceCallback* legacy_callback
        instances = self.active_instances
        already_active = bool(instances)
        instances.add(prof)
        if already_active:
            return
        # Use `sys.monitoring` in Python 3.12 and above;
        # otherwise, use the legacy trace-callback system
        # see: https://docs.python.org/3/library/sys.monitoring.html
        if CAN_USE_SYS_MONITORING:
            self._sys_monitoring_register()
        else:
            legacy_callback = alloc_callback()
            fetch_callback(legacy_callback)
            self.legacy_callback = legacy_callback
            PyEval_SetTrace(legacy_trace_callback, self)

    cpdef _handle_disable_event(self, prof):
        cdef TraceCallback* legacy_callback
        instances = self.active_instances
        instances.discard(prof)
        if instances:
            return
        # Use `sys.monitoring` in Python 3.12 and above;
        # otherwise, use the legacy trace-callback system
        # see: https://docs.python.org/3/library/sys.monitoring.html
        if CAN_USE_SYS_MONITORING:
            self._sys_monitoring_deregister()
        else:
            legacy_callback = self.legacy_callback
            restore_callback(legacy_callback)
            free_callback(legacy_callback)
            self.legacy_callback = NULL

    cpdef _sys_monitoring_register(self):
        # Note: only activating `sys.monitoring` line events for the
        # profiled code objects in `LineProfiler.add_function()` may
        # seem like an obvious optimization, but:
        # - That adds complexity and muddies the logic, because
        #   `.set_local_events()` can only be called if the tool id is
        #   in use (e.g. activated via `.use_tool_id()`), and
        # - That doesn't result in much (< 2%) performance improvement
        #   in tests
        if not _is_main_thread():
            return
        mon = sys.monitoring
        mon.use_tool_id(mon.PROFILER_ID, 'line_profiler')
        # Activate line events
        events = (mon.get_events(mon.PROFILER_ID)
                  | mon.events.LINE
                  | mon.events.PY_RETURN
                  | mon.events.PY_YIELD
                  | mon.events.RAISE
                  | mon.events.RERAISE)
        mon.set_events(mon.PROFILER_ID, events)
        for event_id, callback in [
                (mon.events.LINE, self.handle_line_event),
                (mon.events.PY_RETURN, self.handle_return_event),
                (mon.events.PY_YIELD, self.handle_yield_event),
                (mon.events.RAISE, self.handle_raise_event),
                (mon.events.RERAISE, self.handle_reraise_event)]:
            self.mon_callbacks[event_id] = mon.register_callback(
                mon.PROFILER_ID, event_id, callback)

    cpdef _sys_monitoring_deregister(self):
        if not _is_main_thread():
            return
        mon = sys.monitoring
        mon.free_tool_id(mon.PROFILER_ID)
        cdef dict wrapped_callbacks = self.mon_callbacks
        while wrapped_callbacks:
            event_id, wrapped_callback = wrapped_callbacks.popitem()
            mon.register_callback(mon.PROFILER_ID, event_id, wrapped_callback)

    property wrap_trace:
        def __get__(self):
            return bool(self._wrap_trace)
        def __set__(self, wrap_trace):
            self._wrap_trace = 1 if wrap_trace else 0


cdef class LineProfiler:
    """
    Time the execution of lines of Python code.

    This is the Cython base class for
    :py:class:`line_profiler.line_profiler.LineProfiler`.

    Arguments:
        *functions (types.FunctionType)
            Function objects to be profiled.
        wrap_trace (Optional[bool])
            What to do if there is an existing (non-profiling)
            :py:mod:`sys` trace callback when the profiler is
            :py:meth:`.enable()`-ed:

            :py:const:`True`:
                *Wrap around* said callback: at the end of running our
                trace callback, also run the existing callback.
            :py:const:`False`:
                *Replace* said callback as long as the profiler is
                enabled.
            :py:const:`None` (default):
                For the first instance created, resolves to

                :py:const:`False`
                    If the environment variable
                    :envvar:`LINE_PROFILE_WRAP_TRACE` is undefined, or
                    if it matches any of
                    ``{'', '0', 'off', 'false', 'no'}``
                    (case-insensitive).

                :py:const:`True`
                    Otherwise.

                If there has already been other instances, the value is
                inherited therefrom.

            In any case, when the profiler is :py:meth:`.disable()`-ed,
            it tries to restore the :py:mod:`sys` trace callback (or the
            lack thereof) to the state it was in from when the profiler
            was :py:meth:`.enable()`-ed (but see Notes).

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
        * ``wrap_trace = True`` helps with using
          :py:class:`LineProfiler` cooperatively with other tools, like
          coverage and debugging tools.
        * However, it should be considered experimental and to be used
          at one's own risk -- because tools generally assume that they
          have sole control over system-wide tracing.
        * When setting ``wrap_trace``, it is set process-wide for all
          instances.
        * In general, Python allows for trace callbacks to unset
          themselves, either intentionally (via ``sys.settrace(None)``)
          or if it errors out.  If the wrapped/cached trace callback
          does so, profiling would continue, but:

          * The cached callback is cleared and is no longer called, and
          * The :py:mod:`sys` trace callback is set to :py:const:`None`

          when the profiler is :py:meth:`.disable()`-ed.
        * It is also allowed for the frame-local trace callable
          (:py:attr:`~types.FrameType.f_trace`) to set
          :py:attr:`~types.FrameType.f_trace_lines` to false in a frame
          to disable line events.  If the wrapped/cached trace callback
          does so, profiling would continue, but
          :py:attr:`~types.FrameType.f_trace` will no longer receive
          line events.
    """
    cdef unordered_map[int64, unordered_map[int64, LineTime]] _c_code_map
    # Mapping between thread-id and map of LastTime
    cdef unordered_map[int64, unordered_map[int64, LastTime]] _c_last_time
    cdef public list functions
    cdef public dict code_hash_map, dupes_map
    cdef public double timer_unit
    cdef public object threaddata

    # These are shared between instances and threads
    # type: dict[int, set[LineProfiler]], int = thread id
    _all_thread_states = {}
    # type: dict[bytes, int], bytes = bytecode
    _all_paddings = {}
    # type: dict[int, weakref.WeakSet[LineProfiler]], int = func id
    _all_instances_by_funcs = {}

    def __init__(self, *functions, wrap_trace=None):
        self.functions = []
        self.code_hash_map = {}
        self.dupes_map = {}
        self.timer_unit = hpTimerUnit()
        # Create a data store for thread-local objects
        # https://docs.python.org/3/library/threading.html#thread-local-data
        self.threaddata = threading.local()
        if wrap_trace is not None:
            self.wrap_trace = wrap_trace

        for func in functions:
            self.add_function(func)

    cpdef add_function(self, func):
        """
        Record line profiling information for the given Python function.

        Note:
            This is a low-level method and is intended for
            :py:class:`types.FunctionType`; users should in general use
            :py:meth:`line_profiler.LineProfiler.add_callable` for
            adding general callables and callable wrappers (e.g.
            :py:class:`property`).
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
            func_id = id(func)
        except AttributeError:
            try:
                code = func.__func__.__code__
                func_id = id(func.__func__)
            except AttributeError:
                import warnings
                warnings.warn(
                    "Could not extract a code object for the object %r"
                    % (func,))
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
            base_co_code: bytes
            npad_code: int
            base_co_code, npad_code = multibyte_rstrip(co_code)
            try:
                npad = self._all_paddings[base_co_code]
            except KeyError:
                npad = 0
            self._all_paddings[base_co_code] = max(npad, npad_code) + 1
            try:
                profilers_to_update = self._all_instances_by_funcs[func_id]
                profilers_to_update.add(self)
            except KeyError:
                profilers_to_update = WeakSet({self})
                self._all_instances_by_funcs[func_id] = profilers_to_update
            # Maintain `.dupes_map` (legacy)
            try:
                self.dupes_map[base_co_code].append(code)
            except KeyError:
                self.dupes_map[base_co_code] = [code]
            if npad > npad_code:
                # Code hash already exists, so there must be a duplicate
                # function (on some instance);
                # (re-)pad with no-op
                co_code = base_co_code + NOP_BYTES * npad
                code = _code_replace(func, co_code=co_code)
                try:
                    func.__code__ = code
                except AttributeError as e:
                    func.__func__.__code__ = code
            else:  # No re-padding -> no need to update the other profs
                profilers_to_update = {self}
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
            code = code.replace(co_filename=cython_source)
            profilers_to_update = {self}
        # Update `._c_code_map` and `.code_hash_map` with the new line
        # hashes on `self` (and other instances profiling the same
        # function if we padded the bytecode)
        for instance in profilers_to_update:
            prof = <LineProfiler>instance
            try:
                line_hashes = prof.code_hash_map[code]
            except KeyError:
                line_hashes = prof.code_hash_map[code] = []
            for code_hash in code_hashes:
                line_hash = <int64>code_hash
                if not prof._c_code_map.count(line_hash):
                    line_hashes.append(line_hash)
                    prof._c_code_map[line_hash]

        self.functions.append(func)

    property enable_count:
        def __get__(self):
            if not hasattr(self.threaddata, 'enable_count'):
                self.threaddata.enable_count = 0
            return self.threaddata.enable_count
        def __set__(self, value):
            self.threaddata.enable_count = value

    # These two are shared between instances, but thread-local

    property wrap_trace:
        def __get__(self):
            return self._thread_state.wrap_trace
        def __set__(self, wrap_trace):
            self._thread_state.wrap_trace = wrap_trace

    property _thread_state:
        def __get__(self):
            thread_id = threading.get_ident()
            try:
                return self._all_thread_states[thread_id]
            except KeyError:
                pass

            # First instance, load default `wrap_trace` value from the
            # environment
            # (TODO: migrate to `line_profiler.cli_utils.boolean()`
            # after merging #335)
            from os import environ

            env = environ.get('LINE_PROFILE_WRAP_TRACE', '').lower()
            wrap_trace = env not in {'', '0', 'off', 'false', 'no'}
            self._all_thread_states[thread_id] = state = ThreadState(
                wrap_trace=wrap_trace)
            return state

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

    def enable(self):
        self._thread_state._handle_enable_event(self)

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
            KeyError
                If no profiling data is available on the current thread.
        """
        try:
            return (<dict>self._c_last_time)[threading.get_ident()]
        except KeyError as e:
            # We haven't actually profiled anything yet
            raise (KeyError('No profiling data on the current thread '
                            '(`threading.get_ident()` = '
                            f'{threading.get_ident()})')
                   .with_traceback(e.__traceback__)) from None

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

    cpdef disable(self):
        self._c_last_time[threading.get_ident()].clear()
        self._thread_state._handle_disable_event(self)

    def get_stats(self):
        """
        Returns:
            :py:class:`LineStats` object containing the timings.
        """
        cdef dict cmap = self._c_code_map

        all_entries = {}
        for code in self.code_hash_map:
            entries = []
            for entry in self.code_hash_map[code]:
                entries.extend(cmap[entry].values())
            key = label(code)

            # Merge duplicate line numbers, which occur for branch
            # entrypoints like `if`
            entries_by_lineno = all_entries.setdefault(key, {})

            for line_dict in entries:
                 _, lineno, total_time, nhits = line_dict.values()
                 orig_nhits, orig_total_time = entries_by_lineno.get(
                     lineno, (0, 0))
                 entries_by_lineno[lineno] = (orig_nhits + nhits,
                                              orig_total_time + total_time)

        # Aggregate the timing data
        stats = {
            key: sorted((line, nhits, time)
                        for line, (nhits, time) in entries_by_lineno.items())
            for key, entries_by_lineno in all_entries.items()}
        return LineStats(stats, self.timer_unit)


@cython.boundscheck(False)
@cython.wraparound(False)
cdef inline inner_trace_callback(
        int is_line_event, object instances, object code, int lineno):
    """
    The basic building block for the trace callbacks.
    The :c:func:`PyEval_SetTrace` callback.

    References:
       https://github.com/python/cpython/blob/de2a4036/Include/cpython/\
pystate.h#L16
    """
    cdef object prof_
    cdef object bytecode = code.co_code
    cdef LineProfiler prof
    cdef LastTime old
    cdef int key
    cdef PY_LONG_LONG time
    cdef int has_time = 0
    cdef int64 code_hash
    cdef int64 block_hash
    cdef unordered_map[int64, LineTime] line_entries

    if any(bytecode):
        block_hash = hash(bytecode)
    else:  # Cython functions have empty/zero bytecodes
        block_hash = hash(code)
    code_hash = compute_line_hash(block_hash, lineno)

    for prof_ in instances:
        prof = <LineProfiler>prof_
        if not prof._c_code_map.count(code_hash):
            continue
        if not has_time:
            time = hpTimer()
            has_time = 1
        ident = threading.get_ident()
        if prof._c_last_time[ident].count(block_hash):
            old = prof._c_last_time[ident][block_hash]
            line_entries = prof._c_code_map[code_hash]
            key = old.f_lineno
            if not line_entries.count(key):
                prof._c_code_map[code_hash][key] = LineTime(code_hash, key, 0, 0)
            prof._c_code_map[code_hash][key].nhits += 1
            prof._c_code_map[code_hash][key].total_time += time - old.time
        if is_line_event:
            # Get the time again. This way, we don't record much time wasted
            # in this function.
            prof._c_last_time[ident][block_hash] = LastTime(lineno, hpTimer())
        elif prof._c_last_time[ident].count(block_hash):
            # We are returning from a function, not executing a line. Delete
            # the last_time record. It may have already been deleted if we
            # are profiling a generator that is being pumped past its end.
            prof._c_last_time[ident].erase(prof._c_last_time[ident].find(block_hash))


cdef extern int legacy_trace_callback(
        object state, PyFrameObject *py_frame, int what, PyObject *arg):
    """
    The :c:func:`PyEval_SetTrace` callback.

    References:
       https://github.com/python/cpython/blob/de2a4036/Include/cpython/\
pystate.h#L16
    """
    cdef ThreadState state_ = <ThreadState>state
    if what == PyTrace_LINE or what == PyTrace_RETURN:
        # Normally we'd need to DECREF the return from
        # `get_frame_code()`, but Cython does that for us
        inner_trace_callback((what == PyTrace_LINE),
                             state_.active_instances,
                             <object>get_frame_code(py_frame),
                             PyFrame_GetLineNumber(py_frame))

    # Call the trace callback that we're wrapping around where
    # appropriate
    if state_._wrap_trace:
        return call_callback(state_.legacy_callback, py_frame, what, arg)
    return 0
