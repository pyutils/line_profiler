import asyncio
import contextlib
import functools
import inspect
import io
import sys
import textwrap
import types
import pytest
from line_profiler import LineProfiler


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


class check_timings:
    """
    Verify that the profiler starts without timing data and ends with
    some.
    """
    def __init__(self, prof):
        self.prof = prof

    def __enter__(self):
        timings = self.timings
        assert not any(timings.values()), (
            f'Expected no timing entries, got {timings!r}')
        return self.prof

    def __exit__(self, *_, **__):
        timings = self.timings
        assert any(timings.values()), (
            f'Expected timing entries, got {timings!r}')

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

    with check_timings(profile):
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

    with check_timings(profile):
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

    with check_timings(profile):
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

    with check_timings(profile):
        assert profile.enable_count == 0
        assert asyncio.run(coro_wrapped()) == 1
        assert profile.enable_count == 0


def test_async_gen_decorator():
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

    with check_timings(profile):
        assert profile.enable_count == 0
        assert asyncio.run(use_agen_simple()) == [0]
        assert profile.enable_count == 0
        assert asyncio.run(use_agen_simple(1, 2, 3)) == [0, 1, 3, 6]
        assert profile.enable_count == 0
        assert asyncio.run(use_agen_complex(1, 2, 3)) == [0, 1, 3, 6]
        assert profile.enable_count == 0
        assert asyncio.run(use_agen_complex(1, 2, 3, None, 4)) == [0, 1, 3, 6]
        assert profile.enable_count == 0


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
    with io.StringIO() as sio:
        profile.print_stats(stream=sio, summarize=True)
        output = strip(sio.getvalue())
    print(output)
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
    with io.StringIO() as sio:
        profile.print_stats(stream=sio, summarize=True)
        output = strip(sio.getvalue())
    print(output)
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
    with io.StringIO() as sio:
        profile.print_stats(stream=sio, summarize=True)
        output = strip(sio.getvalue())
    print(output)
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
    with io.StringIO() as sio:
        profile.print_stats(stream=sio, summarize=True)
        output = strip(sio.getvalue())
    print(output)
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
    with io.StringIO() as sio:
        profile.print_stats(stream=sio, summarize=True)
        output = strip(sio.getvalue())
    print(output)
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
    with io.StringIO() as sio:
        profile.print_stats(stream=sio, summarize=True)
        output = strip(sio.getvalue())
    print(output)
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
    with io.StringIO() as sio:
        profile.print_stats(stream=sio, summarize=True)
        output = strip(sio.getvalue())
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
def test_sys_monitoring():
    """
    Test that `LineProfiler` is properly registered with
    `sys.monitoring`.
    """
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

    import io
    s = io.StringIO()
    profiler.print_stats(stream=s)
    output = s.getvalue()

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
