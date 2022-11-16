import pytest
from line_profiler import LineProfiler


def f(x):
    """A docstring."""
    y = x + 10
    return y


def g(x):
    y = yield x + 10
    yield y + 20

class C:
    @classmethod
    def c(self, value):
        print(value)
        return 0


def test_init():
    lp = LineProfiler()
    assert lp.functions == []
    assert lp.code_map == {}
    lp = LineProfiler(f)
    assert lp.functions == [f]
    assert lp.code_map == {f.__code__: {}}
    lp = LineProfiler(f, g)
    assert lp.functions == [f, g]
    assert lp.code_map ==  {
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


def test_function_decorator():
    profile = LineProfiler()
    f_wrapped = profile(f)
    assert f_wrapped.__name__ == 'f'

    assert profile.enable_count == 0
    value = f_wrapped(10)
    assert profile.enable_count == 0
    assert value == f(10)


def test_gen_decorator():
    profile = LineProfiler()
    g_wrapped = profile(g)
    assert g_wrapped.__name__ == 'g'

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

def test_classmethod_decorator():
    profile = LineProfiler()
    c_wrapped = profile(C.c)
    assert c_wrapped.__name__ == 'c'
    assert profile.enable_count == 0
    val = c_wrapped('test')
    assert profile.enable_count == 0
    assert val == C.c('test')
    assert profile.enable_count == 0
