from __future__ import annotations
import asyncio
import contextlib
import functools
import gc
import inspect
import io
import os
import pickle
import sys
import textwrap
import types
from tempfile import TemporaryDirectory
import pytest
from line_profiler import _line_profiler, LineProfiler, LineStats


def f(x):
    """A docstring."""
    y = x + 10
    return y


def g(x):
    y = yield x + 10
    yield y + 20


async def ag(delay, start=()):
    i = 0
    for x in start:
        yield i
        i += x
    while True:
        received = await asyncio.sleep(delay, (yield i))
        if received is None:
            return
        i += received


def get_profiling_tool_name():
    return sys.monitoring.get_tool(sys.monitoring.PROFILER_ID)


def strip(s):
    return textwrap.dedent(s).strip('\n')


def get_prof_stats(prof, name='prof', **kwargs):
    with io.StringIO() as sio:
        prof.print_stats(sio, **kwargs)
        output = sio.getvalue()
        print(f'@{name}:', textwrap.indent(output, '  '), sep='\n\n')
    return output


class check_timings_and_mem:
    """
    Verify that:
    - The profiler starts without timing data and ends with some.
    - We don't leak reference counts for code objects (which are
      retrieved with a C function in the Cython code).
    """
    def __init__(self, prof, *,
                 check_timings=True, check_ref_counts=True, gc=False):
        self.prof = prof
        self.check_timings = bool(check_timings)
        self.check_ref_counts = bool(check_ref_counts)
        self.ref_counts = None
        # Sometimes the garbage collector seem to need extra help;
        # in those cases, explicitly garbage-collect
        self.gc = gc

    def __enter__(self):
        self._check_timings_enter()
        self._check_ref_counts_enter()
        return self.prof

    def __exit__(self, *_, **__):
        self._check_timings_exit()
        self._check_ref_counts_exit()

    def _check_timings_enter(self):
        if not self.check_timings:
            return
        timings = self.timings
        assert not any(timings.values()), (
            f'Expected no timing entries, got {timings!r}')

    def _check_timings_exit(self):
        if not self.check_timings:
            return
        timings = self.timings
        assert any(timings.values()), (
            f'Expected timing entries, got {timings!r}')

    def _check_ref_counts_enter(self):
        if not self.check_ref_counts:
            return
        self.ref_counts = self.get_ref_counts()

    def _check_ref_counts_exit(self):
        if not self.check_ref_counts:
            return
        assert self.ref_counts is not None
        for key, count in self.get_ref_counts().items():
            try:
                referrers = repr(gc.get_referrers(*(
                    code for code in self.prof.code_hash_map
                    if code.co_name == key)))
                msg = (f'{key}(): '
                       f'ref count {self.ref_counts[key]} -> {count} '
                       f'(referrers: {referrers})')
                if self.ref_counts[key] == count:
                    print(msg)
                else:
                    raise AssertionError(msg)
            except KeyError:
                pass

    def get_ref_counts(self):
        if self.gc:
            gc.collect()
        results = {}
        for code in self.prof.code_hash_map:
            # Note: use the name as the key to avoid messing with the
            # ref count
            key = code.co_name
            results[key] = results.get(key, 0) + sys.getrefcount(code)
        return results

    @property
    def timings(self):
        return self.prof.get_stats().timings


def test_init():
    lp = LineProfiler()
    assert lp.functions == []
    assert lp.code_map == {}
    lp = LineProfiler(f)
    assert lp.functions == [f]
    assert lp.code_map == {f.__code__: {}}
    lp = LineProfiler(f, g)
    assert lp.functions == [f, g]
    assert lp.code_map == {
        f.__code__: {},
        g.__code__: {},
    }


def test_last_time():
    """
    Test that `LineProfiler.c_last_time` and `LineProfiler.last_time`
    are consistent.
    """
    prof = LineProfiler()
    with pytest.raises(KeyError, match='[Nn]o profiling data'):
        prof.c_last_time

    def get_last_time(prof, *, c=False):
        try:
            return getattr(prof, 'c_last_time' if c else 'last_time')
        except KeyError:
            return {}

    @prof
    def func():
        return (get_last_time(prof, c=True).copy(),
                get_last_time(prof).copy())

    # These are always empty outside a profiling context
    # (hence the need of the above function to capture the transient
    # values)
    assert not get_last_time(prof, c=True)
    assert not get_last_time(prof)
    # Inside `func()`, both should get an entry therefor
    clt, lt = func()
    assert not get_last_time(prof, c=True)
    assert not get_last_time(prof)
    assert set(clt) == {hash(func.__wrapped__.__code__.co_code)}
    assert set(lt) == {func.__wrapped__.__code__}


def test_enable_disable():
    lp = LineProfiler()
    assert lp.enable_count == 0
    lp.enable_by_count()
    assert lp.enable_count == 1
    lp.enable_by_count()
    assert lp.enable_count == 2
    lp.disable_by_count()
    assert lp.enable_count == 1
    lp.disable_by_count()
    assert lp.enable_count == 0
    assert lp.last_time == {}
    lp.disable_by_count()
    assert lp.enable_count == 0

    with lp:
        assert lp.enable_count == 1
        with lp:
            assert lp.enable_count == 2
        assert lp.enable_count == 1
    assert lp.enable_count == 0
    assert lp.last_time == {}

    with pytest.raises(RuntimeError):
        assert lp.enable_count == 0
        with lp:
            assert lp.enable_count == 1
            raise RuntimeError()
    assert lp.enable_count == 0
    assert lp.last_time == {}


def test_double_decoration():
    """
    Test that wrapping the same function twice does not result in
    spurious profiling entries.
    """
    profile = LineProfiler()
    f_wrapped = profile(f)
    f_double_wrapped = profile(f_wrapped)
    assert f_double_wrapped is f_wrapped

    with check_timings_and_mem(profile):
        assert profile.enable_count == 0
        value = f_wrapped(10)
        assert profile.enable_count == 0
        assert value == f(10)

    assert len(profile.get_stats().timings) == 1


def test_function_decorator():
    """
    Test for `LineProfiler.wrap_function()`.
    """
    profile = LineProfiler()
    f_wrapped = profile(f)
    assert f in profile.functions
    assert f_wrapped.__name__ == 'f'

    with check_timings_and_mem(profile):
        assert profile.enable_count == 0
        value = f_wrapped(10)
        assert profile.enable_count == 0
        assert value == f(10)


def test_gen_decorator():
    """
    Test for `LineProfiler.wrap_generator()`.
    """
    profile = LineProfiler()
    g_wrapped = profile(g)
    assert inspect.isgeneratorfunction(g_wrapped)
    assert g in profile.functions
    assert g_wrapped.__name__ == 'g'

    with check_timings_and_mem(profile):
        assert profile.enable_count == 0
        i = g_wrapped(10)
        assert profile.enable_count == 0
        assert next(i) == 20
        assert profile.enable_count == 0
        assert i.send(30) == 50
        assert profile.enable_count == 0
        with pytest.raises(StopIteration):
            next(i)
        assert profile.enable_count == 0


def test_coroutine_decorator():
    """
    Test for `LineProfiler.wrap_coroutine()`.
    """
    async def coro(delay=.015625):
        return (await asyncio.sleep(delay, 1))

    profile = LineProfiler()
    coro_wrapped = profile(coro)
    assert inspect.iscoroutinefunction(coro)
    assert coro in profile.functions

    with check_timings_and_mem(profile):
        assert profile.enable_count == 0
        assert asyncio.run(coro_wrapped()) == 1
        assert profile.enable_count == 0


@pytest.mark.parametrize('gc', [True, False])
def test_async_gen_decorator(gc):
    """
    Test for `LineProfiler.wrap_async_generator()`.
    """
    delay = .015625

    async def use_agen_complex(*args, delay=delay):
        results = []
        agen = ag_wrapped(delay)
        results.append(await agen.asend(None))  # Start the generator
        for send in args:
            with (pytest.raises(StopAsyncIteration)
                  if send is None else
                  contextlib.nullcontext()):
                results.append(await agen.asend(send))
            if send is None:
                break
        return results

    async def use_agen_simple(*args, delay=delay):
        results = []
        async for i in ag_wrapped(delay, args):
            results.append(i)
        return results

    profile = LineProfiler()
    ag_wrapped = profile(ag)
    assert inspect.isasyncgenfunction(ag_wrapped)
    assert ag in profile.functions

    with check_timings_and_mem(profile):
        assert profile.enable_count == 0
        assert asyncio.run(use_agen_simple()) == [0]
        assert profile.enable_count == 0
        assert asyncio.run(use_agen_simple(1, 2, 3)) == [0, 1, 3, 6]
        assert profile.enable_count == 0
    # FIXME: why does `use_agen_complex()` need the `gc.collect()` to
    # not fail in Python 3.12+? Doesn't seem to matter which
    # ${LINE_PROFILER_CORE} we're using either...
    with contextlib.ExitStack() as stack:
        xfail_312 = hasattr(sys, 'monitoring') and not gc
        if xfail_312:  # Python 3.12+
            excinfo = stack.enter_context(
                pytest.raises(AssertionError, match=r'ag\(\): ref count'))
        stack.enter_context(
            check_timings_and_mem(profile, check_timings=False, gc=gc))
        assert profile.enable_count == 0
        assert asyncio.run(use_agen_complex(1, 2, 3)) == [0, 1, 3, 6]
        assert profile.enable_count == 0
        assert asyncio.run(use_agen_complex(1, 2, 3, None, 4)) == [0, 1, 3, 6]
        assert profile.enable_count == 0
    if xfail_312:
        pytest.xfail('\nsys.version={!r}..., gc={}:\n{}'
                     .format(sys.version.strip().split()[0], gc,
                             excinfo.getrepr(style='no')))


def test_classmethod_decorator():
    """
    Test for `LineProfiler.wrap_classmethod()`.

    Notes
    -----
    This is testing for an edge case;
    for the best result, always use `@profile` as the innermost
    decorator, as auto-profile normally does.
    """
    profile = LineProfiler()

    class Object:
        @profile
        @classmethod
        def foo(cls) -> str:
            return cls.__name__ * 2

    assert isinstance(inspect.getattr_static(Object, 'foo'), classmethod)
    assert profile.enable_count == 0
    assert len(profile.functions) == 1
    assert Object.foo() == Object().foo() == 'ObjectObject'
    output = strip(get_prof_stats(profile, name='profile', summarize=True))
    # Check that we have profiled `Object.foo()`
    assert output.endswith('foo')
    line, = (line for line in output.splitlines() if line.endswith('* 2'))
    # Check that it has been run twice
    assert int(line.split()[1]) == 2
    assert profile.enable_count == 0


def test_staticmethod_decorator():
    """
    Test for `LineProfiler.wrap_staticmethod()`.

    Notes
    -----
    This is testing for an edge case;
    for the best result, always use `@profile` as the innermost
    decorator, as auto-profile normally does.
    """
    profile = LineProfiler()

    class Object:
        @profile
        @staticmethod
        def foo(x: int) -> int:
            return x * 2

    assert isinstance(inspect.getattr_static(Object, 'foo'), staticmethod)
    assert profile.enable_count == 0
    assert len(profile.functions) == 1
    assert Object.foo(3) == Object().foo(3) == 6
    output = strip(get_prof_stats(profile, name='profile', summarize=True))
    # Check that we have profiled `Object.foo()`
    assert output.endswith('foo')
    line, = (line for line in output.splitlines() if line.endswith('* 2'))
    # Check that it has been run twice
    assert int(line.split()[1]) == 2
    assert profile.enable_count == 0


def test_boundmethod_decorator():
    """
    Test for `LineProfiler.wrap_boundmethod()`.

    Notes
    -----
    This is testing for an edge case;
    for the best result, always use `@profile` as the innermost
    decorator, as auto-profile normally does.
    """
    profile = LineProfiler()

    class Object:
        def foo(self, x: int) -> int:
            return id(self) * x

    obj = Object()
    # Check that calls are aggregated
    profiled_foo_1 = profile(obj.foo)
    profiled_foo_2 = profile(obj.foo)
    assert isinstance(profiled_foo_1, types.MethodType)
    assert isinstance(profiled_foo_2, types.MethodType)
    assert profile.enable_count == 0
    # XXX: should we try do remove duplicates?
    assert profile.functions == [Object.foo, Object.foo]
    assert (profiled_foo_1(2)
            == profiled_foo_2(2)
            == obj.foo(2)
            == id(obj) * 2)
    output = strip(get_prof_stats(profile, name='profile', summarize=True))
    # Check that we have profiled `Object.foo()`
    assert output.endswith('foo')
    line, = (line for line in output.splitlines() if line.endswith('* x'))
    # Check that the wrapped methods has been run twice in total
    assert int(line.split()[1]) == 2
    assert profile.enable_count == 0


def test_partialmethod_decorator():
    """
    Test for `LineProfiler.wrap_partialmethod()`

    Notes
    -----
    This is testing for an edge case;
    for the best result, always use `@profile` as the innermost
    decorator in a function definition, as auto-profile normally does.
    """
    profile = LineProfiler()

    class Object:
        def foo(self, x: int) -> int:
            return id(self) * x

        bar = profile(functools.partialmethod(foo, 1))

    assert isinstance(inspect.getattr_static(Object, 'bar'),
                      functools.partialmethod)
    obj = Object()
    assert profile.enable_count == 0
    assert profile.functions == [Object.foo]
    assert obj.foo(1) == obj.bar() == id(obj)
    output = strip(get_prof_stats(profile, name='profile', summarize=True))
    # Check that we have profiled `Object.foo()` (via `.bar()`)
    assert output.endswith('foo')
    line, = (line for line in output.splitlines() if line.endswith('* x'))
    # Check that the wrapped method has been run once
    assert int(line.split()[1]) == 1
    assert profile.enable_count == 0


def test_partial_decorator() -> None:
    """
    Test for `LineProfiler.wrap_partial()`.

    Notes
    -----
    This is testing for an edge case;
    for the best result, always use `@profile` as the innermost
    decorator, as auto-profile normally does.
    """
    profile = LineProfiler()

    def foo(x: int, y: int) -> int:
        return x + y

    bar = functools.partial(foo, 2)
    profiled_bar_1 = profile(bar)
    profiled_bar_2 = profile(bar)
    assert isinstance(profiled_bar_1, functools.partial)
    assert isinstance(profiled_bar_2, functools.partial)
    assert profile.enable_count == 0
    # XXX: should we try do remove duplicates?
    assert profile.functions == [foo, foo]
    assert (profiled_bar_1(3)
            == profiled_bar_2(3)
            == bar(3)
            == foo(2, 3)
            == 5)
    output = strip(get_prof_stats(profile, name='profile', summarize=True))
    # Check that we have profiled `foo()`
    assert output.endswith('foo')
    line, = (line for line in output.splitlines() if line.endswith('x + y'))
    # Check that the wrapped partials has been run twice in total
    assert int(line.split()[1]) == 2
    assert profile.enable_count == 0


def test_property_decorator():
    """
    Test for `LineProfiler.wrap_property()`.

    Notes
    -----
    This is testing for an edge case;
    for the best result, always use `@profile` as the innermost
    decorator, as auto-profile normally does.
    """
    profile = LineProfiler()

    class Object:
        def __init__(self, x: int) -> None:
            self.x = x

        @profile
        @property
        def foo(self) -> int:
            return self.x * 2

        # The profiler sees both the setter and the already-wrapped
        # getter here, but it shouldn't re-wrap the getter

        @profile
        @foo.setter
        def foo(self, foo) -> None:
            self.x = foo // 2

    assert isinstance(Object.foo, property)
    assert profile.enable_count == 0
    assert len(profile.functions) == 2
    obj = Object(3)
    assert obj.foo == 6  # Use getter
    obj.foo = 10  # Use setter
    assert obj.x == 5
    assert obj.foo == 10  # Use getter
    output = strip(get_prof_stats(profile, name='profile', summarize=True))
    # Check that we have profiled `Object.foo`
    assert output.endswith('foo')
    getter_line, = (line for line in output.splitlines()
                    if line.endswith('* 2'))
    setter_line, = (line for line in output.splitlines()
                    if line.endswith('// 2'))
    # Check that the getter has been run twice and the setter once
    assert int(getter_line.split()[1]) == 2
    assert int(setter_line.split()[1]) == 1
    assert profile.enable_count == 0


def test_cached_property_decorator():
    """
    Test for `LineProfiler.wrap_cached_property()`.

    Notes
    -----
    This is testing for an edge case;
    for the best result, always use `@profile` as the innermost
    decorator, as auto-profile normally does.
    """
    profile = LineProfiler()

    class Object:
        def __init__(self, x: int) -> None:
            self.x = x

        @profile
        @functools.cached_property
        def foo(self) -> int:
            return self.x * 2

    assert isinstance(Object.foo, functools.cached_property)
    assert profile.enable_count == 0
    assert len(profile.functions) == 1
    obj = Object(3)
    assert obj.foo == 6  # Use getter
    assert obj.foo == 6  # Getter not called because it's cached
    output = strip(get_prof_stats(profile, name='profile', summarize=True))
    # Check that we have profiled `Object.foo`
    assert output.endswith('foo')
    line, = (line for line in output.splitlines() if line.endswith('* 2'))
    # Check that the getter has been run once
    assert int(line.split()[1]) == 1
    assert profile.enable_count == 0


def test_class_decorator():
    """
    Test for `LineProfiler.wrap_class()`.
    """
    profile = LineProfiler()

    def unrelated(x):
        return str(x)

    @profile
    class Object:
        def __init__(self, x):
            self.x = self.convert(x)

        @property
        def id(self):
            return id(self)

        @classmethod
        def class_method(cls, n):
            return cls.__name__ * n

        # This is unrelated to `Object` and shouldn't be profiled
        convert = staticmethod(unrelated)

    # Are we keeping tabs on the correct entities?
    assert len(profile.functions) == 3
    assert set(profile.functions) == {
        Object.__init__.__wrapped__,
        Object.id.fget.__wrapped__,
        vars(Object)['class_method'].__func__.__wrapped__}
    # Make some calls
    assert not profile.enable_count
    obj = Object(1)
    assert obj.x == '1'
    assert id(obj) == obj.id
    assert obj.class_method(3) == 'ObjectObjectObject'
    assert not profile.enable_count
    # Check the profiling results
    all_entries = sorted(sum(profile.get_stats().timings.values(), []))
    assert len(all_entries) == 3
    assert all(nhits == 1 for (_, nhits, _) in all_entries)


def test_add_class_wrapper():
    """
    Test adding a callable-wrapper object wrapping a class.
    """
    profile = LineProfiler()

    class Object:
        @classmethod
        class method:
            def __init__(self, cls, x):
                self.cls = cls
                self.x = x

            def __repr__(self):
                fmt = '{.__name__}.{.__name__}({!r})'.format
                return fmt(self.cls, type(self), self.x)

    # Bookkeeping
    profile.add_class(Object)
    method_cls = vars(Object)['method'].__func__
    assert profile.functions == [method_cls.__init__, method_cls.__repr__]
    # Actual profiling
    with profile:
        obj = Object.method(1)
        assert obj.cls == Object
        assert obj.x == 1
        assert repr(obj) == 'Object.method(1)'
    # Check data
    all_nhits = {
        func_name.rpartition('.')[-1]: sum(nhits for (_, nhits, _) in entries)
        for (*_, func_name), entries in profile.get_stats().timings.items()}
    assert all_nhits['__init__'] == all_nhits['__repr__'] == 2


@pytest.mark.parametrize('decorate', [True, False])
def test_profiler_c_callable_no_op(decorate):
    """
    Test that the following are no-ops on C-level callables:
    - Decoration (`.__call__()`): the callable is returned as-is.
    - `.add_callable()`: it returns 0.
    """
    profile = LineProfiler()

    for (func, Type) in [
            (len, types.BuiltinFunctionType),
            ('string'.split, types.BuiltinMethodType),
            (vars(int)['from_bytes'], types.ClassMethodDescriptorType),
            (str.split, types.MethodDescriptorType),
            ((1).__str__, types.MethodWrapperType),
            (int.__repr__, types.WrapperDescriptorType)]:
        assert isinstance(func, Type)
        if decorate:  # Decoration is no-op
            assert profile(func) is func
        else:  # Add is no-op
            assert not profile.add_callable(func)
        assert not profile.functions


def test_show_func_column_formatting():
    from line_profiler.line_profiler import show_func
    import line_profiler
    import io
    # Use a function in this module as an example
    func = line_profiler.line_profiler.show_text
    start_lineno = func.__code__.co_firstlineno
    filename = func.__code__.co_filename
    func_name = func.__name__

    def get_func_linenos(func):
        import sys
        if sys.version_info[0:2] >= (3, 10):
            return sorted(set([t[0] if t[2] is None else t[2]
                               for t in func.__code__.co_lines()]))
        else:
            import dis
            return sorted(set([t[1] for t in dis.findlinestarts(func.__code__)]))
    line_numbers = get_func_linenos(func)

    unit = 1.0
    output_unit = 1.0
    stripzeros = False

    # Build fake timeings for each line in the example function
    timings = [
        (lineno, idx * 1e13, idx * (2e10 ** (idx % 3)))
        for idx, lineno in enumerate(line_numbers, start=1)
    ]
    stream = io.StringIO()
    show_func(filename, start_lineno, func_name, timings, unit,
              output_unit, stream, stripzeros)
    text = stream.getvalue()
    print(text)

    timings = [
        (lineno, idx * 1e15, idx * 2e19)
        for idx, lineno in enumerate(line_numbers, start=1)
    ]
    stream = io.StringIO()
    show_func(filename, start_lineno, func_name, timings, unit,
              output_unit, stream, stripzeros)
    text = stream.getvalue()
    print(text)

    # TODO: write a check to verify columns are aligned nicely


@pytest.mark.skipif(not hasattr(sys, 'monitoring'),
                    reason='no `sys.monitoring` in version '
                    f'{".".join(str(v) for v in sys.version_info[:2])}')
def test_sys_monitoring(monkeypatch):
    """
    Test that `LineProfiler` is properly registered with
    `sys.monitoring`.
    """
    monkeypatch.setattr(_line_profiler, 'USE_LEGACY_TRACE', False)
    profile = LineProfiler()
    get_name_wrapped = profile(get_profiling_tool_name)
    tool = get_profiling_tool_name()
    assert tool is None, (
        f'Expected no active profiling tool before profiling, got {tool!r}'
    )
    tool = get_name_wrapped()
    assert tool == 'line_profiler', (
        "Expected 'line_profiler' to be registered with `sys.monitoring` "
        f'when a profiled function is run, got {tool!r}'
    )
    tool = get_profiling_tool_name()
    assert tool is None, (
        f'Expected no active profiling tool after profiling, got {tool!r}'
    )


def test_profile_generated_code():
    """
    Test that profiling shows source information with generated code.
    """
    import linecache
    from line_profiler import LineProfiler
    from line_profiler.line_profiler import is_generated_code

    # Simulate generated code in linecache

    # Note: this test will fail if the generated code name does not
    # start with "<generated: ".
    generated_code_name = "<generated: 'test_fn'>"
    assert is_generated_code(generated_code_name)

    code_lines = [
        "def test_fn():",
        "    return 42"
    ]

    linecache.cache[generated_code_name] = (None, None, [l + "\n" for l in code_lines], None)

    # Compile the generated code
    ns = {}
    exec(compile("".join(l + "\n" for l in code_lines), generated_code_name, "exec"), ns)
    fn = ns["test_fn"]

    # Profile the generated function
    profiler = LineProfiler()
    profiled_fn = profiler(fn)
    profiled_fn()

    output = get_prof_stats(profiler, 'profiler')

    # Check that the output contains the generated code's source lines
    for line in code_lines:
        assert line in output

    # .. as well as the generated code name
    assert generated_code_name in output


def test_multiple_profilers_usage():
    """
    Test using more than one profilers simultaneously.
    """
    prof1 = LineProfiler()
    prof2 = LineProfiler()

    def sum_n(n):
        x = 0
        for n in range(1, n + 1):
            x += n
        return x

    @prof1
    def sum_n_sq(n):
        x = 0
        for n in range(1, n + 1):
            x += n ** 2
        return x

    @prof2
    def sum_n_cb(n):
        x = 0
        for n in range(1, n + 1):
            x += n ** 3
        return x

    # If we decorate a wrapper, just "register" the profiler with the
    # existing wrapper and add the wrapped function
    sum_n_wrapper_1 = prof1(sum_n)
    assert prof1.functions == [sum_n_sq.__wrapped__, sum_n]
    sum_n_wrapper_2 = prof2(sum_n_wrapper_1)
    assert sum_n_wrapper_2 is not sum_n_wrapper_1
    assert prof2.functions == [sum_n_cb.__wrapped__, sum_n]

    # Call the functions
    n = 400
    assert sum_n_wrapper_1(n) == .5 * n * (n + 1)
    assert sum_n_wrapper_2(n) == .5 * n * (n + 1)
    assert 6 * sum_n_sq(n) == n * (n + 1) * (2 * n + 1)
    assert sum_n_cb(n) == .25 * (n * (n + 1)) ** 2

    # Inspect the timings
    t1 = {fname.rpartition('.')[-1]: entries
          for (*_, fname), entries in prof1.get_stats().timings.items()}
    t2 = {fname.rpartition('.')[-1]: entries
          for (*_, fname), entries in prof2.get_stats().timings.items()}
    assert set(t1) == {'sum_n_sq', 'sum_n'}
    assert set(t2) == {'sum_n_cb', 'sum_n'}
    # Note: `prof1` active when both wrapper is called, but `prof2` only
    # when `sum_n_wrapper_2()` is
    assert t1['sum_n'][2][1] == 2 * n
    assert t2['sum_n'][2][1] == n
    assert t1['sum_n_sq'][2][1] == n
    assert t2['sum_n_cb'][2][1] == n


def test_duplicate_code_objects():
    """
    Test that results are correctly aggregated between duplicate code
    objects.
    """
    code = textwrap.dedent("""
    @profile
    def func(n):
        x = 0
        for n in range(1, n + 1):
            x += n
        return x
    """).strip('\n')
    profile = LineProfiler()
    # Create and call the function once
    namespace_1 = {'profile': profile}
    exec(code, namespace_1)
    assert 'func' in namespace_1
    assert len(profile.functions) == 1
    assert namespace_1['func'].__wrapped__ in profile.functions
    assert namespace_1['func'](10) == 10 * 11 // 2
    # Do it again
    namespace_2 = {'profile': profile}
    exec(code, namespace_2)
    assert 'func' in namespace_2
    assert len(profile.functions) == 2
    assert namespace_2['func'].__wrapped__ in profile.functions
    assert namespace_2['func'](20) == 20 * 21 // 2
    # Check that data from both calls are aggregated
    # (Entries are represented as tuples `(lineno, nhits, time)`)
    entries, = profile.get_stats().timings.values()
    assert entries[-2][1] == 10 + 20


@pytest.mark.parametrize('force_same_line_numbers', [True, False])
@pytest.mark.parametrize(
    'ops',
    [
        # Replication of the problematic case in issue #350
        'func1:prof_all'
        '-func2:prof_some:prof_all'
        '-func3:prof_all'
        '-func4:prof_some:prof_all',
        # Invert the order of decoration
        'func1:prof_all'
        '-func2:prof_all:prof_some'
        '-func3:prof_all'
        '-func4:prof_all:prof_some',
        # More profiler stacks
        'func1:p1:p2'
        '-func2:p2:p3'
        '-func3:p3:p4'
        '-func4:p4:p1',
        'func1:p1:p2:p3'
        '-func2:p2:p3:p4'
        '-func3:p3:p4:p1'
        '-func4:p4:p1:p2',
        'func1:p1:p2:p3'
        '-func2:p4:p3:p2'
        '-func3:p3:p4:p1'
        '-func4:p2:p1:p4',
        # Misc. edge cases
        # - Naive padding of the following case would cause `func1()`
        #   and `func2()` to end up with the same bytecode, so guard
        #   against it
        'func1:p1:p2'  # `func1()` padded once?
        '-func2:p3'  # `func2()` padded twice?
        '-func1:p4:p3',  # `func1()` padded once (again)?
        # - Check that double decoration doesn't mess things up
        'func1:p1:p2'
        '-func2:p2:p3'
        '-func3:p3:p4'
        '-func4:p4:p1'
        '-func1:p1',  # Now we're passing `func1()` to `p1` twice
    ])
def test_multiple_profilers_identical_bytecode(
        tmp_path, ops, force_same_line_numbers):
    """
    Test that functions compiling down to the same bytecode are
    correctly handled between multiple profilers.

    Notes
    -----
    - `ops` should consist of chunks joined by hyphens, where each chunk
      has the format `<func_id>:<prof_name>[:<prof_name>[...]]`,
      indicating that the profilers are to be used in order to decorate
      the specified function.
    - `force_same_line_numbers` is used to coerce all functions to
      compile down to code objects with the same line numbers.
    """
    def check_seen(name, output, func_id, expected):
        lines = [line for line in output.splitlines()
                 if line.startswith('Function: ')]
        if any(func_id in line for line in lines) == expected:
            return
        if expected:
            raise AssertionError(
                f'profiler `@{name}` didn\'t see `{func_id}()`')
        raise AssertionError(
            f'profiler `@{name}` saw `{func_id}()`')

    def check_has_profiling_data(name, output, func_id, expected):
        assert func_id.startswith('func')
        nloops = func_id[len('func'):]
        try:
            line = next(line for line in output.splitlines()
                        if line.endswith(f'result.append({nloops})'))
        except StopIteration:
            if expected:
                raise AssertionError(
                    f'profiler `@{name}` didn\'t see `{func_id}()`')
            else:
                return
        if (line.split()[1] == nloops) == expected:
            return
        if expected:
            raise AssertionError(
                f'profiler `@{name}` didn\'t get data from `{func_id}()`')
        raise AssertionError(
            f'profiler `@{name}` got data from `{func_id}()`')

    if force_same_line_numbers:
        funcs = {}
        pattern = strip("""
        def func{0}():
            result = []
            for _ in range({0}):
                result.append({0})
            return result
            """)
        for i in [1, 2, 3, 4]:
            tempfile = tmp_path / f'func{i}.py'
            source = pattern.format(i)
            tempfile.write_text(source)
            exec(compile(source, str(tempfile), 'exec'), funcs)
    else:
        def func1():
            result = []
            for _ in range(1):
                result.append(1)
            return result

        def func2():
            result = []
            for _ in range(2):
                result.append(2)
            return result

        def func3():
            result = []
            for _ in range(3):
                result.append(3)
            return result

        def func4():
            result = []
            for _ in range(4):
                result.append(4)
            return result

        funcs = {'func1': func1, 'func2': func2,
                 'func3': func3, 'func4': func4}

    # Apply the decorators in order
    all_dec_names = {f'func{i}': set() for i in [1, 2, 3, 4]}
    all_profs = {}
    for op in ops.split('-'):
        func_id, *profs = op.split(':')
        all_dec_names[func_id].update(profs)
        for name in profs:
            try:
                prof = all_profs[name]
            except KeyError:
                prof = all_profs[name] = LineProfiler()
            funcs[func_id] = prof(funcs[func_id])
    # Call each function once
    assert funcs['func1']() == [1]
    assert funcs['func2']() == [2, 2]
    assert funcs['func3']() == [3, 3, 3]
    assert funcs['func4']() == [4, 4, 4, 4]
    # Check that the bytecodes of the profiled functions are distinct
    profiled_funcs = {funcs[name].__line_profiler_id__.func
                      for name, decs in all_dec_names.items() if decs}
    assert len({func.__code__.co_code
                for func in profiled_funcs}) == len(profiled_funcs)
    # Check the profiling results
    for name, prof in sorted(all_profs.items()):
        output = get_prof_stats(prof, name=name, summarize=True)
        for func_id, decs in all_dec_names.items():
            profiled = name in decs
            check_seen(name, output, func_id, profiled)
            check_has_profiling_data(name, output, func_id, profiled)


def test_aggregate_profiling_data_between_code_versions():
    """
    Test that profiling data from previous versions of the code object
    are preserved when another profiler causes the code object of a
    function to be overwritten.
    """
    def func(n):
        x = 0
        for n in range(1, n + 1):
            x += n
        return x

    prof1 = LineProfiler()
    prof2 = LineProfiler()

    # Gather data with `@prof1`
    wrapper1 = prof1(func)
    assert wrapper1(10) == 10 * 11 // 2
    code = func.__code__
    # Gather data with `@prof2`; the code object is overwritten here
    wrapper2 = prof2(wrapper1)
    assert func.__code__ != code
    assert wrapper2(15) == 15 * 16 // 2
    # Despite the overwrite of the code object, the old data should
    # still remain, and be aggregated with the new data when calling
    # `prof1.get_stats()`
    for prof, name, count in (prof1, 'prof1', 25), (prof2, 'prof2', 15):
        result = get_prof_stats(prof, name)
        loop_body = next(line for line in result.splitlines()
                         if line.endswith('x += n'))
        assert loop_body.split()[1] == str(count)


@pytest.mark.xfail(condition=sys.version_info[:2] == (3, 9),
                   reason='Handling of `finally` bugged in Python 3.9')
def test_profiling_exception():
    """
    Test that profiling data is reported for:
    - The line raising an exception
    - The last lines in the `except` and `finally` subblocks of a
      `try`-(`except`-)`finally` statement

    Notes
    -----
    Seems to be bugged for Python 3.9 only; may be related to CPython
    issue #83295.
    """
    prof = LineProfiler()

    class MyException(Exception):
        pass

    @prof
    def func_raise():
        pass
        raise MyException  # Raise: raise
        l.append(0)

    @prof
    def func_try_finally():
        try:
            raise MyException  # Try-finally: try
        finally:
            l.append(1)  # Try-finally: finally

    @prof
    def func_try_except_finally(reraise):
        try:
            raise MyException  # Try-except-finally: try
        except MyException:
            l.append(2)  # Try-except-finally: except
            if reraise:
                raise
        finally:
            l.append(3)  # Try-except-finally: finally

    l = []
    for func in [func_raise, func_try_finally,
                 functools.partial(func_try_except_finally, True),
                 functools.partial(func_try_except_finally, False)]:
        try:
            func()
        except MyException:
            pass
    result = get_prof_stats(prof)
    assert l == [1, 2, 3, 2, 3]
    for stmt, nhits in [
            ('raise', 1), ('try-finally', 1), ('try-except-finally', 2)]:
        for step in stmt.split('-'):
            comment = '# {}: {}'.format(stmt.capitalize(), step)
            line = next(line for line in result.splitlines()
                        if line.endswith(comment))
            assert line.split()[1] == str(nhits)


@pytest.mark.parametrize('n', [1, 2])
@pytest.mark.parametrize('legacy', [True, False])
def test_load_stats_files(legacy, n):
    """
    Test the loading of stats files. If ``legacy`` is true, the
    tempfiles are written from
    :py:class:`line_profiler._line_profiler.LineStats` objects instead
    of  :py:class:`line_profiler.line_profiler.LineStats` objects, so
    that we ensure that ``'.lprof'`` files written by old versions of
    :py:mod:`line_profiler` is still properly handled.
    """
    def write(stats, filename):
        if legacy:
            legacy_stats = type(stats).__base__(stats.timings, stats.unit)
            assert not isinstance(legacy_stats, LineStats)
            with open(filename, mode='wb') as fobj:
                pickle.dump(legacy_stats, fobj)
        else:
            stats.to_file(filename)
        return filename

    stats1 = LineStats({('foo', 1, 'spam.py'): [(2, 3, 3600)]}, .015625)
    stats2 = LineStats({('foo', 1, 'spam.py'): [(2, 4, 700)],
                        ('bar', 10, 'spam.py'): [(10, 20, 1000)]},
                       .0625)
    with TemporaryDirectory() as tmpdir:
        fname1 = write(stats1, os.path.join(tmpdir, '1.lprof'))
        if n == 1:
            stats_combined = stats1
            files = [fname1]
        else:
            fname2 = write(stats2, os.path.join(tmpdir, '2.lprof'))
            stats_combined = stats1 + stats2
            files = [fname1, fname2]
        stats_read = LineStats.from_files(*files)
    assert isinstance(stats_read, LineStats)
    assert stats_read == stats_combined
