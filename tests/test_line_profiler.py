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
    Test for `LineProfiler.wrap_cached_property()`

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


def test_multiple_profilers_metadata():
    """
    Test the curation of profiler metadata (e.g. `.code_hash_map`,
    `.dupes_map`, `.code_map`) from the underlying C-level profiler.
    """
    from copy import deepcopy
    from operator import attrgetter
    from warnings import warn

    prof1 = LineProfiler()
    prof2 = LineProfiler()
    cprof = prof1._get_c_profiler()
    assert prof2._get_c_profiler() is cprof

    @prof1
    @prof2
    def f(c=False):
        get_time = attrgetter('c_last_time' if c else 'last_time')
        t1 = get_time(prof1)
        t2 = get_time(prof2)
        return [t1, t2, get_time(cprof)]

    @prof1
    def g():
        return [prof1.enable_count, prof2.enable_count]

    @prof2
    def h():  # Same bytecode as `g()`
        return [prof1.enable_count, prof2.enable_count]

    get_code = attrgetter('__wrapped__.__code__')

    # `.functions`
    assert prof1.functions == [f.__wrapped__, g.__wrapped__]
    assert prof2.functions == [f.__wrapped__, h.__wrapped__]
    # `.enable_count`
    # (Note: `.enable_count` is automatically in-/de-cremented in
    # decorated functions, so we need to access it within a called
    # function)
    assert g() == [1, 0]
    assert h() == [0, 1]
    assert prof1.enable_count == prof2.enable_count == cprof.enable_count == 0
    # `.timer_unit`
    assert prof1.timer_unit == prof2.timer_unit == cprof.timer_unit
    # `.code_hash_map`
    assert set(prof1.code_hash_map) == {get_code(f), get_code(g)}
    assert set(prof2.code_hash_map) == {get_code(f), get_code(h)}

    # `.c_code_map`
    prof1_line_hashes = {h for hashes in prof1.code_hash_map.values()
                         for h in hashes}
    assert set(prof1.c_code_map) == prof1_line_hashes
    prof2_line_hashes = {h for hashes in prof2.code_hash_map.values()
                         for h in hashes}
    assert set(prof2.c_code_map) == prof2_line_hashes
    # `.code_map`
    assert set(prof1.code_map) == {get_code(f), get_code(g)}
    assert len(prof1.code_map[get_code(f)]) == 0
    assert len(prof1.code_map[get_code(g)]) == 1
    assert set(prof2.code_map) == {get_code(f), get_code(h)}
    assert len(prof2.code_map[get_code(f)]) == 0
    assert len(prof2.code_map[get_code(h)]) == 1
    t1, t2, _ = f()  # Timing info gathered after calling the function
    assert len(prof1.code_map[get_code(f)]) == 4  # 4 real lines
    assert len(prof2.code_map[get_code(f)]) == 4

    # `.c_last_time`
    # (Note: `.c_last_time` is transient, so we need to access it within
    # a called function)
    ct1, ct2, _ = f(c=True)
    assert set(ct1) == set(ct2) == {hash(get_code(f).co_code)}
    # `.last_time`
    # (Note: `.last_time` is currently bugged; since `.c_last_time`
    # stores code-block hashes and `.code_hash_map` line hashes,
    # `line_profiler._line_profiler.LineProfiler.last_time` never gets a
    # hash match and is thus always empty)
    t1, t2, tc = f(c=False)
    if tc:
        expected = {get_code(f)}
    else:
        msg = ('`line_profiler/_line_profiler.pyx::LineProfiler.last_time` '
               'is always empty because `.c_last_time` and `.code_hash_map` '
               'use different types of hashes (see PR #344)')
        warn(msg, DeprecationWarning)
        expected = set()
    assert set(t1) == set(t2) == set(tc) == expected

    # `.dupes_map` (introduce a dupe for this)
    # Note: `h.__wrapped__.__code__` is padded but the `.dupes_map`
    # entries are not
    assert prof1.dupes_map == {get_code(f).co_code: [get_code(f)],
                               get_code(g).co_code: [get_code(g)]}
    h = prof1(h)
    dupes = deepcopy(prof1.dupes_map)
    h_code = dupes[get_code(g).co_code][-1]
    assert get_code(h).co_code.startswith(h_code.co_code)
    dupes[get_code(g).co_code][-1] = (h_code
                                      .replace(co_code=get_code(h).co_code))
    assert dupes == {get_code(f).co_code: [get_code(f)],
                     get_code(g).co_code: [get_code(g), get_code(h)]}


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
    sum_n_wrapper = prof1(sum_n)
    assert prof1.functions == [sum_n_sq.__wrapped__, sum_n]
    sum_n_wrapper_2 = prof2(sum_n_wrapper)
    assert prof2.functions == [sum_n_cb.__wrapped__, sum_n]
    assert sum_n_wrapper_2 is sum_n_wrapper

    # Call the functions
    n = 400
    assert sum_n_wrapper(n) == .5 * n * (n + 1)
    assert 6 * sum_n_sq(n) == n * (n + 1) * (2 * n + 1)
    assert sum_n_cb(n) == .25 * (n * (n + 1)) ** 2

    # Inspect the timings
    t1 = {fname: entries
          for (*_, fname), entries in prof1.get_stats().timings.items()}
    t2 = {fname: entries
          for (*_, fname), entries in prof2.get_stats().timings.items()}
    assert set(t1) == {'sum_n_sq', 'sum_n'}
    assert set(t2) == {'sum_n_cb', 'sum_n'}
    assert t1['sum_n'][2][1] == t2['sum_n'][2][1] == n
    assert t1['sum_n_sq'][2][1] == n
    assert t2['sum_n_cb'][2][1] == n
