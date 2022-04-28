
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
    import sys
    from line_profiler import LineProfiler

    num_iters = 100
    sleep_time = 0.01

    def async_run(future):
        # For Python 3.6
        if sys.version_info[0:2] >= (3, 7):
            asyncio.run(future)
        else:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(asyncio.wait([future]))

    def report_results(prefix, timer_raw, timer_prof):
        time2 = async_timer_raw.elapsed
        profile.print_stats()

        time1 = timer_raw.elapsed
        time2 = timer_prof.elapsed

        ideal_time = num_iters * sleep_time
        max_time = max(time2, time1)
        min_time = min(time2, time1)
        ratio = max_time / min_time
        error = abs(max_time - ideal_time)

        lstats = profile.get_stats()
        unit = lstats.unit
        stats = lstats.timings

        profiled_items = sorted(stats.items())
        for (fn, lineno, name), timings in profiled_items:
            total_time = 0.0
            for lineno, nhits, time_ in timings:
                total_time += time_

        print(f'{prefix} ideal_time={ideal_time}')
        print(f'{prefix} time1={time1}')
        print(f'{prefix} time2={time2}')
        print(f'{prefix} unit={unit}')
        print(f'{prefix} error={error}')
        print(f'{prefix} ratio={ratio}')
        print(f'{prefix} total_time={total_time}')

        assert ratio < 2.0, 'profiled function should run about as fast'
        assert error < (ideal_time * 0.5), 'should be somewhat close to the ideal time'
        assert len(profiled_items) == 1

    # Async version of the function
    async def async_function():
        for idx in range(num_iters):
            await asyncio.sleep(sleep_time)

    # Sync version of the function
    def sync_function():
        for idx in range(num_iters):
            time.sleep(sleep_time)

    # --- test async version
    with Timer() as async_timer_raw:
        async_run(async_function())

    profile = LineProfiler()
    profiled_async_function = profile(async_function)
    with Timer() as async_timer_prof:
        async_run(profiled_async_function())

    # --- test sync version

    with Timer() as sync_timer_raw:
        sync_function()

    profile = LineProfiler()
    profiled_sync_function = profile(sync_function)
    with Timer() as sync_timer_prof:
        profiled_sync_function()

    report_results('sync', sync_timer_raw, sync_timer_prof)
    report_results('async', async_timer_raw, async_timer_prof)


if __name__ == '__main__':
    """
    CommandLine:
        python tests/test_async.py
    """
    test_async_profile()
