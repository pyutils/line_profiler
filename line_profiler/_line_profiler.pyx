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
from collections.abc import Callable
from functools import wraps
import sys
cimport cython
from cython.operator cimport dereference as deref
from cpython.object cimport PyObject_Hash
from cpython.bytes cimport PyBytes_AS_STRING, PyBytes_GET_SIZE
from cpython.exc cimport PyErr_Clear
from cpython.version cimport PY_VERSION_HEX
from libc.stdint cimport int64_t, intptr_t
from libc.stdlib cimport free, malloc

from libcpp.unordered_map cimport unordered_map
from libcpp.vector cimport vector
import functools
import threading
import types
from warnings import warn

from line_profiler._diagnostics import (
    WRAP_TRACE, SET_FRAME_LOCAL_TRACE, USE_LEGACY_TRACE
)

from ._map_helpers cimport LastTime, LastTimeMap, LineTime, LineTimeMap
from ._profiler_base cimport (
    _LineProfilerBase, compute_line_hash, note_events_disabled,
    restart_disabled_events,
)
from ._sys_monitoring cimport _SysMonitoringState

# Most of the non-tracing code lives in these modules, so that the
# extension modules can be compiled in parallel; re-export what used to
# be defined here
from ._profiler_base import (  # noqa: F401
    NOP_VALUE, NOP_BYTES_LEN, NOP_BYTES, HAS_CO_QUALNAME,
    _CAN_USE_SYS_MONITORING, CANNOT_LINE_TRACE_CYTHON,
    label, find_cython_source_file, _code_replace, _copy_local_sysmon_events,
)
from ._sys_monitoring import _patch_events  # noqa: F401

if not (USE_LEGACY_TRACE or _CAN_USE_SYS_MONITORING):
    # Shouldn't happen since we're already checking the existence of
    # `sys.monitoring` in `line_profiler._diagnostics`, but just to be
    # absolutely sure...
    warn("`sys.monitoring`-based line profiling selected but unavailable "
         f"in Python {sys.version}; falling back to the legacy trace system")
    USE_LEGACY_TRACE = True

from ._profiler_base cimport int64, uint64

cdef extern from "Python_wrapper.h":
    ctypedef struct PyObject
    ctypedef struct PyCodeObject
    ctypedef struct PyFrameObject
    ctypedef Py_ssize_t Py_hash_t
    ctypedef long long PY_LONG_LONG
    ctypedef int (*Py_tracefunc)(
        object self, PyFrameObject *py_frame, int what, PyObject *arg)

    cdef PyCodeObject* PyFrame_GetCode(PyFrameObject* frame)
    cdef int PyCode_Addr2Line(PyCodeObject *co, int byte_offset)

    cdef void PyEval_SetTrace(Py_tracefunc func, object arg)
    cdef PyObject* PyObject_Call(
        PyObject *callable, PyObject *args, PyObject *kwargs) except *

    # They're actually #defines, but whatever.
    cdef int PyTrace_CALL
    cdef int PyTrace_EXCEPTION
    cdef int PyTrace_LINE
    cdef int PyTrace_RETURN
    cdef int PyTrace_OPCODE
    cdef int PyTrace_C_CALL
    cdef int PyTrace_C_EXCEPTION
    cdef int PyTrace_C_RETURN

    cdef int PyFrame_GetLineNumber(PyFrameObject *frame)
    cdef void Py_INCREF(PyObject *o)
    cdef void Py_XDECREF(PyObject *o)
    cdef void *get_current_frame_key()

    cdef unsigned long PyThread_get_thread_ident()

    ctypedef void (*freefunc)(void *)
    cdef Py_ssize_t PyUnstable_Eval_RequestCodeExtraIndex(freefunc free)
    cdef int PyUnstable_Code_GetExtra(object code, Py_ssize_t index,
                                      void **extra)
    cdef int PyUnstable_Code_SetExtra(object code, Py_ssize_t index,
                                      void *extra)

ctypedef PyCodeObject *PyCodeObjectPtr
#ctypedef unordered_map[int64, LastTime] LastTimeMap
#ctypedef unordered_map[int64, LineTime] LineTimeMap

cdef extern from "c_trace_callbacks.h":  # Legacy tracing
    ctypedef unsigned long long Py_uintptr_t

    ctypedef struct TraceCallback:
        Py_tracefunc c_tracefunc
        PyObject *c_traceobj

    cdef TraceCallback *alloc_callback() except *
    cdef void free_callback(TraceCallback *callback)
    cdef void populate_callback(TraceCallback *callback)
    cdef void restore_callback(TraceCallback *callback)
    cdef int call_callback(
        PyObject *disabler, TraceCallback *callback,
        PyFrameObject *py_frame, int what, PyObject *arg)
    cdef void set_local_trace(PyObject *manager, PyFrameObject *py_frame)
    cdef Py_uintptr_t monitoring_restart_version()

cdef extern from "timers.c":
    PY_LONG_LONG hpTimer()
    double hpTimerUnit()

#cdef struct LineTime:
#    int64 code
#    int lineno
#    PY_LONG_LONG total_time
#    long nhits

#cdef struct LastTime:
#    int f_lineno
#    PY_LONG_LONG time


# sys.monitoring event ids, looked up once here rather than on every
# event in the callbacks
cdef int _LINE_EVENT = 0
cdef int _PY_RETURN_EVENT = 0
cdef int _PY_YIELD_EVENT = 0
cdef int _RAISE_EVENT = 0
cdef int _RERAISE_EVENT = 0
cdef object _DISABLE = None
if _CAN_USE_SYS_MONITORING:
    _DISABLE = sys.monitoring.DISABLE
    _LINE_EVENT = sys.monitoring.events.LINE
    _PY_RETURN_EVENT = sys.monitoring.events.PY_RETURN
    _PY_YIELD_EVENT = sys.monitoring.events.PY_YIELD
    _RAISE_EVENT = sys.monitoring.events.RAISE
    _RERAISE_EVENT = sys.monitoring.events.RERAISE


cdef struct BlockInfo:
    Py_hash_t block_hash
    # Whether it's Cython code (which has no bytecode of its own)
    bint is_cython


cdef void free_block_info(void *info) noexcept nogil:
    free(info)


# Index of the per-code-object scratch slot (see PEP 523) in which
# get_block_info() caches a BlockInfo; negative if none is available
cdef Py_ssize_t _BLOCK_INFO_EXTRA_INDEX = (
    PyUnstable_Eval_RequestCodeExtraIndex(free_block_info))
if _BLOCK_INFO_EXTRA_INDEX < 0:
    PyErr_Clear()


cdef inline void compute_block_info(object code, BlockInfo *info) noexcept:
    """
    Compute the hash identifying the code block, which is the hash of
    the bytecode for Python code and of the code object for Cython code.
    """
    cdef object py_bytes_obj = code.co_code
    cdef char* data = PyBytes_AS_STRING(py_bytes_obj)
    cdef Py_ssize_t size = PyBytes_GET_SIZE(py_bytes_obj)

    # Loop over every byte to check if any are not NULL
    # if there are any non-NULL, that indicates we're profiling Python code
    for i in range(size):
        if data[i]:
            info.block_hash = hash(py_bytes_obj)
            info.is_cython = False
            return
    # fallback for Cython functions
    info.block_hash = hash(code)
    info.is_cython = True


cdef inline BlockInfo *get_block_info(
        object code, BlockInfo *scratch) noexcept:
    """
    compute_block_info(), but cached on the code object so that
    the trace callbacks don't have to fetch and scan its bytecode for
    every event.

    Returns:
        The cached info, or scratch (filled in) if it can't be
        cached
    """
    cdef void *extra = NULL
    cdef BlockInfo *info
    if _BLOCK_INFO_EXTRA_INDEX >= 0:
        PyUnstable_Code_GetExtra(code, _BLOCK_INFO_EXTRA_INDEX, &extra)
        if extra != NULL:
            return <BlockInfo *>extra
        info = <BlockInfo *>malloc(sizeof(BlockInfo))
        if info != NULL:
            compute_block_info(code, info)
            if PyUnstable_Code_SetExtra(
                    code, _BLOCK_INFO_EXTRA_INDEX, <void *>info) == 0:
                return info
            PyErr_Clear()
            scratch[0] = info[0]
            free(info)
            return scratch
    compute_block_info(code, scratch)
    return scratch


def disable_line_events(trace_func: Callable) -> Callable:
    """
    Returns:
        trace_func (Callable)
            If it is a wrapper created by
            :py:attr:`_LineProfilerManager.wrap_local_f_trace`;
            ``trace_func.disable_line_events`` is also set to true
        wrapper (Callable)
            Otherwise, a thin wrapper around ``trace_func()`` which
            withholds line events.

    Note:
        This is for when a frame-local :py:attr:`~frame.f_trace`
        disables :py:attr:`~frame.f_trace_lines` -- we would like to
        keep line events enabled (so that line profiling works) while
        "unsubscribing" the trace function from it.
    """
    @wraps(trace_func)
    def wrapper(frame, event, arg):
        if event == 'line':
            return
        return trace_func(frame, event, arg)

    try:  # Disable the wrapper directly
        if hasattr(trace_func, '__line_profiler_manager__'):
            trace_func.disable_line_events = True
            return trace_func
    except AttributeError:
        pass
    return wrapper


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


cdef class _LineProfilerManager:
    """
    Helper object for managing the thread-local state.
    Supports being called with the same signature as a legacy trace
    function (see :py:func:`sys.settrace`).

    Other methods of interest:

    :py:meth:`~.handle_line_event`
        Callback for |LINE|_ events
    :py:meth:`~.handle_return_event`
        Callback for |PY_RETURN|_ events
    :py:meth:`~.handle_yield_event`
        Callback for |PY_YIELD|_ events
    :py:meth:`~.handle_raise_event`
        Callback for |RAISE|_ events
    :py:meth:`~.handle_reraise_event`
        Callback for |RERAISE|_ events

    Note:
        Documentations are for reference only, and all APIs are to be
        considered private and subject to change.

    .. |LINE| replace:: :py:attr:`!sys.monitoring.events.LINE`
    .. |PY_RETURN| replace:: :py:attr:`!sys.monitoring.events.PY_RETURN`
    .. |PY_YIELD| replace:: :py:attr:`!sys.monitoring.events.PY_YIELD`
    .. |RAISE| replace:: :py:attr:`!sys.monitoring.events.RAISE`
    .. |RERAISE| replace:: :py:attr:`!sys.monitoring.events.RERAISE`
    .. _LINE: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-LINE
    .. _PY_RETURN: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-PY_RETURN
    .. _PY_YIELD: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-PY_YIELD
    .. _RAISE: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-RAISE
    .. _RERAISE: https://docs.python.org/3/library/\
sys.monitoring.html#monitoring-event-RERAISE
    """
    cdef TraceCallback *legacy_callback
    cdef _SysMonitoringState mon_state
    cdef public set active_instances  # type: set[LineProfiler]
    # Mirrors .active_instances (holding references of its own) for
    # the trace callbacks, which can iterate over it more cheaply
    cdef vector[PyObject *] _active
    cdef int _wrap_trace
    cdef int _set_frame_local_trace
    cdef int recursion_guard

    def __init__(
            self, tool_id: int, wrap_trace: bool, set_frame_local_trace: bool):
        """
        Arguments:
            tool_id (int)
                Tool ID for use with :py:mod:`sys.monitoring`.
            wrap_trace (bool)
                Whether to wrap around legacy and
                :py:mod:`sys.monitoring` trace functions and call them.
            set_frame_local_trace (bool)
                If using the legacy trace system, whether to insert the
                instance as a frame's :py:attr:`~frame.f_trace` upon
                entering a function scope.

        See also:
            :py:class:`~.LineProfiler`
        """
        self.legacy_callback = NULL
        self.mon_state = _SysMonitoringState(tool_id)

        self.active_instances = set()
        self.wrap_trace = wrap_trace
        self.set_frame_local_trace = set_frame_local_trace
        self.recursion_guard = 0

    def __dealloc__(self):
        self._clear_active()

    cdef void _clear_active(self) noexcept:
        cdef PyObject *prof
        for prof in self._active:
            Py_XDECREF(prof)
        self._active.clear()

    cdef void _sync_active(self) noexcept:
        """
        Update self._active to match
        self.active_instances.
        """
        self._clear_active()
        for prof in self.active_instances:
            Py_INCREF(<PyObject *>prof)
            self._active.push_back(<PyObject *>prof)

    @cython.profile(False)
    def __call__(self, frame: types.FrameType, event: str, arg):
        """
        Calls |legacy_trace_callback|_.  If :py:func:`sys.gettrace`
        returns this instance, replaces the default C-level trace
        function |trace_trampoline|_ with |legacy_trace_callback|_ to
        reduce overhead.

        Returns:
            self (_LineProfilerManager):
                This instance.

        .. |legacy_trace_callback| replace:: \
:c:func:`!legacy_trace_callback`
        .. |trace_trampoline| replace:: :c:func:`!trace_trampoline`
        .. _legacy_trace_callback: https://github.com/pyutils/\
line_profiler/blob/main/line_profiler/_line_profiler.pyx
        .. _trace_trampoline: https://github.com/python/cpython/blob/\
6cb20a219a860eaf687b2d968b41c480c7461909/Python/sysmodule.c#L1124
        """
        cdef int what = {'call': PyTrace_CALL,
                         'exception': PyTrace_EXCEPTION,
                         'line': PyTrace_LINE,
                         'return': PyTrace_RETURN,
                         'opcode': PyTrace_OPCODE}[event]
        if not self.recursion_guard:
            # Prevent recursion (e.g. when `.wrap_trace` and
            # `.set_frame_local_trace` are both true)
            legacy_trace_callback(self, <PyFrameObject *>frame,
                                  what, <PyObject *>arg)
        # Set the C-level trace callback back to
        # `legacy_trace_callback()` where appropriate, so that future
        # calls can bypass this `.__call__()` method
        if sys.gettrace() is self:
            PyEval_SetTrace(legacy_trace_callback, self)
        return self

    def wrap_local_f_trace(self, trace_func: Callable) -> Callable:
        """
        Arguments:
            trace_func (Callable[[frame, str, Any], Any])
                Frame-local trace function, presumably set by another
                global trace function.

        Returns:
            wrapper (Callable[[frame, str, Any], Any])
                Thin wrapper around ``trace_func()`` which calls it,
                calls the instance, then returns the return value of
                ``trace_func()``.  This helps prevent other frame-local
                trace functions from displacing the instance when its
                :py:attr:`~.set_frame_local_trace` is true.

        Note:
            * The ``.__line_profiler_manager__`` attribute of the
              returned wrapper is set to the instance.
            * Line events are not passed to the wrapped callable if
              ``wrapper.disable_line_events`` is set to true.
        """
        @wraps(trace_func)
        def wrapper(frame, event, arg):
            if wrapper.disable_line_events and event == 'line':
                result = None
            else:
                result = trace_func(frame, event, arg)
            self(frame, event, arg)
            return result

        wrapper.__line_profiler_manager__ = self
        wrapper.disable_line_events = False
        try:  # Unwrap the wrapper
            if trace_func.__line_profiler_manager__ is self:
                trace_func = trace_func.__wrapped__
        except AttributeError:
            pass
        return wrapper

    # If we allowed these `sys.monitoring` callbacks to be profiled
    # (i.e. to emit line events), we may fall into an infinite recusion;
    # so disable profiling for them pre-emptively
    # Note: these are non-binding def methods (i.e. builtin methods,
    # like those of builtin types) because those are cheaper for
    # sys.monitoring to call than Cython function objects

    @cython.profile(False)
    @cython.binding(False)
    def handle_line_event(self, object code, int lineno):
        """
        Line-event callback for :py:attr:`!sys.monitoring.events.LINE`,
        passed to :py:func:`sys.monitoring.register_callback`.
        """
        if inner_trace_callback(1, &self._active, code, lineno, NULL):
            if self._wrap_trace:
                self.mon_state.call_callback(
                    _LINE_EVENT, code, (lineno,), ())
            return
        if self._wrap_trace:
            self.mon_state.call_callback(_LINE_EVENT, code, (lineno,), ())
            return
        # Not being profiled: stop the event from firing here at all
        # (unless the wrapped callbacks still need it)
        note_events_disabled()
        return _DISABLE

    @cython.profile(False)
    @cython.binding(False)
    def handle_return_event(
            self, object code, int instruction_offset, object retval):
        """
        Return-event callback for :py:attr:`!sys.monitoring.events.PY_RETURN`,
        passed to :py:func:`sys.monitoring.register_callback`.
        """
        if self._handle_exit_event(
                _PY_RETURN_EVENT, code, instruction_offset, retval):
            note_events_disabled()
            return _DISABLE

    @cython.profile(False)
    @cython.binding(False)
    def handle_yield_event(
            self, object code, int instruction_offset, object retval):
        """
        Yield-event callback for :py:attr:`!sys.monitoring.events.PY_YIELD`,
        passed to :py:func:`sys.monitoring.register_callback`.
        """
        if self._handle_exit_event(
                _PY_YIELD_EVENT, code, instruction_offset, retval):
            note_events_disabled()
            return _DISABLE

    @cython.profile(False)
    @cython.binding(False)
    def handle_raise_event(
            self, object code, int instruction_offset, object exception):
        """
        Raise-event callback for :py:attr:`!sys.monitoring.events.RAISE`,
        passed to :py:func:`sys.monitoring.register_callback`.
        """
        self._handle_exit_event(
            _RAISE_EVENT, code, instruction_offset, exception)

    @cython.profile(False)
    @cython.binding(False)
    def handle_reraise_event(
            self, object code, int instruction_offset, object exception):
        """
        Re-raise-event callback for :py:attr:`!sys.monitoring.events.RERAISE`,
        passed to :py:func:`sys.monitoring.register_callback`.
        """
        self._handle_exit_event(
            _RERAISE_EVENT, code, instruction_offset, exception)

    cdef bint _handle_exit_event(
            self, int event_id, object code, int offset, object obj) noexcept:
        """
        Base for the frame-exit-event (e.g. via returning or yielding)
        callbacks passed to :py:func:`sys.monitoring.register_callback`.

        Returns:
            Whether the event can be disabled at the location, i.e. the
            code isn't being profiled and no wrapped callbacks need it

        Note:
            This is deliberately made a non-traceable C method so that
            we don't fall info infinite recursion.
        """
        cdef bint profiled = inner_trace_callback(
            0, &self._active, code,
            PyCode_Addr2Line(<PyCodeObject*>code, offset), NULL)
        if self._wrap_trace:
            self.mon_state.call_callback(event_id, code, (offset,), (obj,))
            return False
        return not profiled

    cpdef _handle_enable_event(self, prof):
        cdef TraceCallback* legacy_callback
        instances = self.active_instances
        already_active = bool(instances)
        if not USE_LEGACY_TRACE and prof not in instances:
            # The code profiled by prof may have had its events
            # disabled while it wasn't being profiled
            restart_disabled_events()
        instances.add(prof)
        self._sync_active()
        if already_active:
            return
        if USE_LEGACY_TRACE:
            legacy_callback = alloc_callback()
            populate_callback(legacy_callback)
            self.legacy_callback = legacy_callback
            PyEval_SetTrace(legacy_trace_callback, self)
        else:
            self.mon_state.register(self.handle_line_event,
                                    self.handle_return_event,
                                    self.handle_yield_event,
                                    self.handle_raise_event,
                                    self.handle_reraise_event)

    cpdef _handle_disable_event(self, prof):
        cdef TraceCallback* legacy_callback
        instances = self.active_instances
        instances.discard(prof)
        self._sync_active()
        if instances:
            return
        # Only use the legacy trace-callback system if Python < 3.12 or
        # if explicitly requested with `LINE_PROFILER_CORE=legacy`;
        # otherwise, use `sys.monitoring`
        # see: https://docs.python.org/3/library/sys.monitoring.html
        if USE_LEGACY_TRACE:
            legacy_callback = self.legacy_callback
            restore_callback(legacy_callback)
            free_callback(legacy_callback)
            self.legacy_callback = NULL
        else:
            if self.mon_state.name is not None:
                # Another tool is using the tool ID; let it receive the
                # events we disabled for ourselves again
                restart_disabled_events()
            self.mon_state.deregister()

    property wrap_trace:
        def __get__(self):
            return bool(self._wrap_trace)
        def __set__(self, wrap_trace):
            self._wrap_trace = 1 if wrap_trace else 0

    property set_frame_local_trace:
        def __get__(self):
            return bool(self._set_frame_local_trace)
        def __set__(self, set_frame_local_trace):
            # Note: as noted in `LineProfiler.__doc__`, there is no
            # point in tempering with `.f_trace` when using
            # `sys.monitoring`... so just set it to false
            self._set_frame_local_trace = (
                1 if set_frame_local_trace and USE_LEGACY_TRACE else 0)


cdef class LineProfiler(_LineProfilerBase):
    """
    Time the execution of lines of Python code.

    This is the Cython base class for
    :py:class:`line_profiler.line_profiler.LineProfiler`.

    Arguments:
        *functions (function)
            Function objects to be profiled.
        wrap_trace (bool | None)
            What to do for existing :py:mod:`sys` trace callbacks when
            an instance is :py:meth:`.enable`-ed:

            :py:data:`True`:
                *Wrap around* said callbacks: when our profiling trace
                callbacks run, they call the corresponding existing
                callbacks (where applicable).
            :py:data:`False`:
                *Suspend* said callbacks as long as
                :py:class:`LineProfiler` instances are enabled.
            :py:data:`None` (default):
                For the first instance created, resolves to

                :py:data:`True`
                    If the environment variable
                    :envvar:`LINE_PROFILER_WRAP_TRACE` is set to any of
                    ``{'1', 'on', 'true', 'yes'}`` (case-insensitive).

                :py:data:`False`
                    Otherwise.

                If other instances already exist, the value is inherited
                therefrom.

            In any case, when all instances are :py:meth:`.disable`-ed,
            the :py:mod:`sys` trace system is restored to the state from
            when the first instance was :py:meth:`.enable`-ed.
            See the :ref:`caveats <warning-trace-caveats>` and also the
            :ref:`extra explanation <note-wrap_trace>`.
        set_frame_local_trace (bool | None)
            When using the
            :ref:`"legacy" trace system <note-backends>`), what to do
            when entering a function or code block (i.e. an event of
            type :c:data:`PyTrace_CALL` or ``'call'`` is encountered)
            when an instance is :py:meth:`.enable`-ed:

            :py:data:`True`:
                Set the frame's :py:attr:`~frame.f_trace` to
                an object associated with the profiler.
            :py:data:`False`:
                Don't do so.
            :py:data:`None` (default):
                For the first instance created, resolves to

                :py:data:`True`
                    If the environment variable
                    :envvar:`LINE_PROFILER_SET_FRAME_LOCAL_TRACE` is set
                    to any of ``{'1', 'on', 'true', 'yes'}``
                    (case-insensitive).

                :py:data:`False`
                    Otherwise.

                If other instances already exist, the value is inherited
                therefrom.

            See the :ref:`caveats <warning-trace-caveats>` and also the
            :ref:`extra explanation <note-set_frame_local_trace>`.

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

    .. _warning-trace-caveats:

    Warning:
        * Setting :py:attr:`.wrap_trace` and/or
          :py:attr:`.set_frame_local_trace` helps with using
          :py:class:`LineProfiler` cooperatively with other tools, like
          coverage and debugging tools, especially when using the
          :ref:`"legacy" trace system <note-backends>`.  However, these
          parameters should be considered **experimental** and to be
          used at one's own risk -- because tools generally assume that
          they have sole control over system-wide tracing (if using
          "legacy" tracing), or at least over the
          :py:mod:`sys.monitoring` tool ID it acquired.
        * When setting :py:attr:`.wrap_trace` and
          :py:attr:`.set_frame_local_trace`, they are set process-wide
          for all instances.

    .. _note-backends:

    Note:
        There are two "cores"/"backends" for :py:class:`LineProfiler`
        between which users can choose:

        ``'new'``, ``'sys.monitoring'``, or ``'sysmon'``
            Use :py:mod:`sys.monitoring` events and callbacks.  Only
            available on (and is the default for) Python 3.12 and newer.
        ``'old'``, ``'legacy'``, or ``'ctrace'``
            Use the `"legacy" trace system`_ (:py:func:`sys.gettrace`,
            :py:func:`sys.settrace`, and :c:func:`PyEval_SetTrace`).
            Default for Python < 3.12.

        Where both cores are available, the user can choose between the
        two by supplying a suitable value to the environment variable
        :envvar:`LINE_PROFILER_CORE`.

    .. _note-wrap_trace:

    Note:
        More on :py:attr:`.wrap_trace`:

        * In general, Python allows for trace callbacks to unset
          themselves, either intentionally (via
          ``sys.settrace(None)`` or
          ``sys.monitoring.register_callback(..., None)``) or if they
          error out.  If the wrapped/cached trace callbacks do so,
          profiling would continue, but:

          * The cached callbacks are cleared and are no longer called,
            and
          * The trace callbacks are unset when all profiler instances
            are :py:meth:`.disable`-ed.
        * If a wrapped/cached frame-local
          :ref:`"legacy" trace callable <note-backends>`
          (:py:attr:`~frame.f_trace`) sets
          :py:attr:`~frame.f_trace_lines` to false in a frame to
          disable local line events, :py:attr:`~.frame.f_trace_lines`
          is restored (so that profiling can continue), but said
          callable will no longer receive said events.
        * Likewise, wrapped/cached :py:mod:`sys.monitoring` callbacks
          can also disable events:

          * At *specific code locations* by returning
            :py:data:`sys.monitoring.DISABLE`;
          * By calling :py:func:`sys.monitoring.set_events` and
            changing the *global event set*; or
          * By calling :py:func:`sys.monitoring.register_callback` and
            *replacing itself* with alternative callbacks (or
            :py:data:`None`).

          When that happens, said disabling acts are again suitably
          intercepted so that line profiling continues, but:

          * Said callbacks will no longer receive the corresponding
            events, and
          * The :py:mod:`sys.monitoring` callbacks and event set are
            updated correspondingly when all profiler instances are
            :py:meth:`.disable`-ed.

          Note that:

          * As with when line profiling is not used, if 
            :py:func:`sys.monitoring.restart_events` is called, the list
            of code locations where events are suppressed is cleared,
            and the wrapped/cached callbacks will once again receive
            events from the
            previously-:py:data:`~sys.monitoring.DISABLE`-d locations.
          * Callbacks which only listen to and alter code-object-local
            events (via :py:func:`sys.monitoring.set_local_events`) do
            not interfere with line profiling, and such changes are
            therefore not intercepted.

    .. _note-set_frame_local_trace:

    Note:
        More on :py:attr:`.set_frame_local_trace`:

        * Since frame-local trace functions is no longer a useful
          concept in the new :py:mod:`sys.monitoring`-based system
          (see also the :ref:`Note on "cores" <note-backends>`), the
          parameter/attribute always resolves to :py:data:`False` when
          using the new :py:class:`LineProfiler` core.
        * With the :ref:`"legacy" trace system <note-backends>`, when
          :py:class:`LineProfiler` instances are :py:meth:`.enable`-ed,
          :py:func:`sys.gettrace` returns an object which manages
          profiling on the thread between all active profiler instances.
          Said object has the same call signature as callables that
          :py:func:`sys.settrace` takes, so that pure-Python code which
          temporarily overrides the trace callable (e.g.
          :py:meth:`doctest.DocTestRunner.run`) can function with
          profiling.  After the object is restored with
          :py:func:`sys.settrace` by said code:

          * If :py:attr:`set_frame_local_trace` is true, line
            profiling resumes *immediately*, because the object has
            already been set to the frame's :py:attr:`~frame.f_trace`.
          * However, if :py:attr:`set_frame_local_trace` is false,
            line profiling only resumes *upon entering another code
            block* (e.g. by calling a callable), because trace
            callables set via :py:func:`sys.settrace` is only called
            for ``'call'`` events (see the `C implementation`_ of
            :py:mod:`sys`).

    .. _C implementation: https://github.com/python/cpython/blob/\
6cb20a219a860eaf687b2d968b41c480c7461909/Python/sysmodule.c#L1124
    .. _"legacy" trace system: https://github.com/python/cpython/blob/\
3.13/Python/legacy_tracing.c
    """
    # These are shared between instances and threads
    if _CAN_USE_SYS_MONITORING:
        # Note: just in case we ever need to override this, e.g. for
        # testing
        tool_id = sys.monitoring.PROFILER_ID  # type: ClassVar[int]
    else:
        # Note: the value doesn't matter here... but set it to be
        # consistent (because the value is public API)
        tool_id = 2
    # type: ClassVar[dict[int, _LineProfilerManager]], int = thread id
    _managers = {}

    def __init__(self, *functions,
                 wrap_trace=None, set_frame_local_trace=None):
        self.functions = []
        self.code_hash_map = {}
        self.dupes_map = {}
        self.timer_unit = hpTimerUnit()
        # Create a data store for thread-local objects
        # https://docs.python.org/3/library/threading.html#thread-local-data
        self.threaddata = threading.local()
        if wrap_trace is not None:
            self.wrap_trace = wrap_trace
        if set_frame_local_trace is not None:
            self.set_frame_local_trace = set_frame_local_trace

        for func in functions:
            self.add_function(func)

    # These three are shared between instances, but thread-local
    # (Ideally speaking they could've been class attributes...)

    property wrap_trace:
        def __get__(self):
            return self._manager.wrap_trace
        def __set__(self, wrap_trace):
            # Make sure we have a manager
            manager = self._manager
            # Sync values between all thread states
            for manager in self._managers.values():
                manager.wrap_trace = wrap_trace

    property set_frame_local_trace:
        def __get__(self):
            return self._manager.set_frame_local_trace
        def __set__(self, set_frame_local_trace):
            # Make sure we have a manager
            manager = self._manager
            # Sync values between all thread states
            for manager in self._managers.values():
                manager.set_frame_local_trace = set_frame_local_trace

    property _manager:
        def __get__(self):
            thread_id = PyThread_get_thread_ident()
            try:
                return self._managers[thread_id]
            except KeyError:
                pass
            # First profiler instance on the thread, get the correct
            # `wrap_trace` and `set_frame_local_trace` values and set up
            # a `_LineProfilerManager`
            try:
                manager, *_ = self._managers.values()
            except ValueError:
                # First thread in the interpretor: load default values
                # from the environment (at package startup time)
                wrap_trace = WRAP_TRACE
                set_frame_local_trace = SET_FRAME_LOCAL_TRACE
            else:
                # Fetch the values from an existing manager
                wrap_trace = manager.wrap_trace
                set_frame_local_trace = manager.set_frame_local_trace
            self._managers[thread_id] = manager = _LineProfilerManager(
                self.tool_id, wrap_trace, set_frame_local_trace)
            return manager

    def enable(self):
        self._manager._handle_enable_event(self)

    cpdef disable(self):
        self._c_last_time[PyThread_get_thread_ident()].clear()
        self._manager._handle_disable_event(self)

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
cdef inline bint inner_trace_callback(
        int is_line_event, vector[PyObject *] *instances, object code,
        int lineno, PyFrameObject *py_frame):
    """
    The basic building block for the trace callbacks.

    Returns:
        Whether the location is profiled by any of the instances
    """
    cdef PyObject *prof_
    cdef LineProfiler prof
    cdef LastTime* old
    cdef PY_LONG_LONG time = 0
    cdef bint has_time = False
    cdef bint has_last
    cdef int64 code_hash
    cdef int64 last_time_key = 0
    cdef unsigned long ident = 0
    cdef BlockInfo scratch
    # because we use Python functions like hash, we CANNOT mark this function as nogil
    cdef BlockInfo *block_info = get_block_info(code, &scratch)
    cdef Py_hash_t block_hash = block_info.block_hash
    cdef LineTime* entry
    cdef LineTimeMap* line_entries
    cdef LastTimeMap* last_map
    cdef unordered_map[int64, LineTimeMap].iterator line_it
    cdef LastTimeMap.iterator last_it

    code_hash = compute_line_hash(block_hash, lineno)

    for prof_ in deref(instances):
        prof = <LineProfiler>prof_
        # Each map is searched once, and the iterator reused, instead of
        # checking with .count() and then indexing into it again
        line_it = prof._c_code_map.find(code_hash)
        if line_it == prof._c_code_map.end():
            continue
        if not has_time:
            time = hpTimer()
            ident = PyThread_get_thread_ident()
            has_time = True
            # last_time_key identifies whose last-line record to use.
            # for Python code, we use the executing frame so that the
            # records of recursive calls to a function don't overwrite
            # those of the calls further up the stack
            # for Cython code under sys.monitoring, we use the code
            # block, since Cython functions don't get frames of their
            # own there (so the times of recursive calls of Cython
            # functions are still lost)
            # A frame address can theoretically equal the hash of an
            # unrelated Cython code block, but that can't misattribute
            # time bc (a record left by another code block is discarded
            # as stale below), at worst the two drop each other's records
            if py_frame != NULL:
                last_time_key = <int64><intptr_t>py_frame
            elif block_info.is_cython:
                last_time_key = <int64>block_hash
            else:
                last_time_key = <int64><intptr_t>get_current_frame_key()
        last_map = &(prof._c_last_time[ident])
        # deref() is Cython's version of the -> accessor in C++. if we don't use deref then
        # Cython thinks that when we index last_map,
        # we want pointer indexing (which is not the case)
        last_it = deref(last_map).find(last_time_key)
        # a record left by a frame of another function at the same
        # address is stale
        has_last = (last_it != deref(last_map).end()
                    and deref(last_it).second.block_hash == block_hash)
        if has_last:
            old = &deref(last_it).second
            line_entries = &deref(line_it).second
            # Indexing creates a zeroed entry if there isn't one yet, so
            # this gets or creates it in a single lookup
            # Note: explicitly `deref()`-ing here causes the new values
            # to be assigned to a temp var;
            # meanwhile, directly dot-accessing a pointer causes Cython
            # to correctly write `ptr->attr = (ptr->attr + incr)`
            entry = &deref(line_entries)[old.f_lineno]
            entry.code = code_hash
            entry.lineno = old.f_lineno
            entry.nhits += 1
            entry.total_time += time - old.time
            if is_line_event:
                # Get the time again. This way, we don't record much
                # time wasted in this function.
                old.f_lineno = lineno
                old.time = hpTimer()
            else:
                # We are returning from a function, not executing a
                # line. Delete the last_time record.
                deref(last_map).erase(last_it)
        elif is_line_event:
            # (Replacing the stale record, if any)
            deref(last_map)[last_time_key] = LastTime(
                lineno, hpTimer(), block_hash)
        elif last_it != deref(last_map).end():
            # Returning from a function with a stale record; delete it
            # all the same
            deref(last_map).erase(last_it)
        # Otherwise there is no last_time record to delete; it may have
        # already been deleted if we are profiling a generator that is
        # being pumped past its end.
    # (We only get the time if some instance is profiling the location)
    return has_time


cdef extern int legacy_trace_callback(
        object manager, PyFrameObject *py_frame, int what, PyObject *arg):
    """
    The :c:func:`PyEval_SetTrace` callback.

    References:
       https://github.com/python/cpython/blob/de2a4036/Include/cpython/\
pystate.h#L16
    """
    cdef _LineProfilerManager manager_ = <_LineProfilerManager>manager
    cdef int result
    cdef int recursion_guard = manager_.recursion_guard
    cdef PyObject *code

    if what == PyTrace_CALL:
        # Any code using the `sys.gettrace()`-`sys.settrace()` paradigm
        # (e.g. to temporarily suspend or alter tracing) will cause line
        # events to not be passed to the global trace callback (i.e.
        # `manager`) for the rest of the frame, e.g.
        #     >>> callback = sys.gettrace()
        #     >>> sys.settrace(None)
        #     >>> try:  # Tracing suspended here
        #     ...     ...
        #     ... finally:
        #     ...     # Tracing object restored here, but line event
        #     ...     # tracing is disabled before the next function
        #     ...     # call because that's what the default trace
        #     ...     # trampoline works
        #     ...     sys.settrace(callback)
        # To circumvent this, set the local `.f_trace` upon entering a
        # frame (if not already set), so that tracing can restart upon
        # the restoration with `sys.settrace()`
        if manager_._set_frame_local_trace:
            set_local_trace(<PyObject *>manager_, py_frame)
    elif what == PyTrace_LINE or what == PyTrace_RETURN:
        code = <PyObject *>PyFrame_GetCode(py_frame)
        inner_trace_callback((what == PyTrace_LINE),
                             &manager_._active,
                             <object>code,
                             PyFrame_GetLineNumber(py_frame),
                             py_frame)
        Py_XDECREF(code)

    # Call the trace callback that we're wrapping around where
    # appropriate
    if manager_._wrap_trace:
        # Due to how the frame-local callback could be set to the active
        # `_LineProfilerManager` or a wrapper object (see
        # `set_local_trace()`), wrap the callback call to make sure that
        # we don't recurse back here
        manager_.recursion_guard = 1
        try:
            result = call_callback(
                <PyObject *>disable_line_events, manager_.legacy_callback,
                py_frame, what, arg)
        finally:
            manager_.recursion_guard = recursion_guard
    else:
        result = 0

    # Prevent other trace functions from overwritting `manager`;
    # if there is a frame-local trace function, create a wrapper calling
    # both it and `manager`
    if manager_._set_frame_local_trace:
        set_local_trace(<PyObject *>manager_, py_frame)
    return result
