
class Timer:
    def __init__(self):
        import time
        self.counter = time.perf_counter

    def __enter__(self):
        self.start = self.counter()
        return self

    def __exit__(self, a, b, c):
        self.elapsed = self.counter() - self.start


def test_async_profile():
    import asyncio
    import time
    from line_profiler import LineProfiler

    n = 100
    m = 0.01

    async def async_function():
        for idx in range(n):
            await asyncio.sleep(m)

    with Timer() as t:
        asyncio.run(async_function())
    time1 = t.elapsed

    profile = LineProfiler()
    profiled_async_function = profile(async_function)
    with Timer() as t:
        asyncio.run(profiled_async_function())
    time2 = t.elapsed
    profile.print_stats()

    ideal_time = n * m
    max_time = max(time2, time1)
    min_time = min(time2, time1)
    ratio = max_time / min_time
    error = abs(max_time - ideal_time)
    assert ratio < 1.5, 'profiled function should run about as fast'
    assert error < (ideal_time * 0.5), 'should be somewhat close to the ideal time'

    lstats = profile.get_stats()
    unit = lstats.unit
    stats = lstats.timings

    profiled_items = sorted(stats.items())
    assert len(profiled_items) == 1
    for (fn, lineno, name), timings in profiled_items:
        total_time = 0.0
        for lineno, nhits, time_ in timings:
            total_time += time_
    print(f'async ideal_time={ideal_time}')
    print(f'async time1={time1}')
    print(f'async time2={time2}')
    print(f'async unit={unit}')
    print(f'async error={error}')
    print(f'async ratio={ratio}')
    print(f'async total_time={total_time}')

    # --- similar test with sync

    def sync_function():
        for idx in range(n):
            time.sleep(m)

    with Timer() as t:
        sync_function()
    time1 = t.elapsed

    profile = LineProfiler()
    profiled_sync_function = profile(sync_function)
    with Timer() as t:
        profiled_sync_function()
    time2 = t.elapsed
    profile.print_stats()

    ideal_time = n * m

    max_time = max(time2, time1)
    min_time = min(time2, time1)
    ratio = max_time / min_time
    error = abs(max_time - ideal_time)

    assert ratio < 1.5, 'profiled function should run about as fast'
    assert error < (ideal_time * 0.5), 'should be somewhat close to the ideal time'

    lstats = profile.get_stats()
    unit = lstats.unit
    stats = lstats.timings

    profiled_items = sorted(stats.items())
    assert len(profiled_items) == 1
    for (fn, lineno, name), timings in profiled_items:
        total_time = 0.0
        for lineno, nhits, time_ in timings:
            total_time += time_
    print(f'sync ideal_time={ideal_time}')
    print(f'sync time1={time1}')
    print(f'sync time2={time2}')
    print(f'sync unit={unit}')
    print(f'sync error={error}')
    print(f'sync ratio={ratio}')
    print(f'sync total_time={total_time}')


if __name__ == '__main__':
    """
    CommandLine:
        python tests/test_async.py
    """
    test_async_profile()
