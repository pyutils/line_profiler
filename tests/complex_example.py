"""
A script used in test_complex_case.py
"""
import line_profiler
import atexit


profile = line_profiler.LineProfiler()


@atexit.register
def _show_profile_on_end():
    profile.print_stats(summarize=1, sort=1, stripzeros=1)


@profile
def fib(n):
    a, b = 0, 1
    while a < n:
        a, b = b, a + b


@profile
def fib_only_called_by_thread(n):
    a, b = 0, 1
    while a < n:
        a, b = b, a + b


@profile
def fib_only_called_by_process(n):
    a, b = 0, 1
    while a < n:
        a, b = b, a + b
    # FIXME: having two functions with the EXACT same code can cause issues
    a = 'no longer exactly the same'


@profile
def main():
    """
    Run a lot of different Fibonacci jobs
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--serial_size', type=int, default=10)
    parser.add_argument('--thread_size', type=int, default=10)
    parser.add_argument('--process_size', type=int, default=10)
    args = parser.parse_args()

    for i in range(args.serial_size):
        fib(i)
        funcy_fib(
            i)
        fib(i)

    from concurrent.futures import ThreadPoolExecutor
    executor = ThreadPoolExecutor(max_workers=4)
    with executor:
        jobs = []
        for i in range(args.thread_size):
            job = executor.submit(fib, i)
            jobs.append(job)

            job = executor.submit(funcy_fib, i)
            jobs.append(job)

            job = executor.submit(fib_only_called_by_thread, i)
            jobs.append(job)

        for job in jobs:
            job.result()

    from concurrent.futures import ProcessPoolExecutor
    executor = ProcessPoolExecutor(max_workers=4)
    with executor:
        jobs = []
        for i in range(args.process_size):
            job = executor.submit(fib, i)
            jobs.append(job)

            job = executor.submit(funcy_fib, i)
            jobs.append(job)

            job = executor.submit(fib_only_called_by_process, i)
            jobs.append(job)

        for job in jobs:
            job.result()


@profile
def funcy_fib(n):
    """
    Alternatite fib function where code splits out over multiple lines
    """
    a, b = (
        0, 1
    )
    while a < n:
        # print(
        #     a, end=' ')
        a, b = b, \
                a + b
    # print(
    # )


if __name__ == '__main__':
    """
    CommandLine:
        cd ~/code/line_profiler/tests/
        python complex_example.py --size 10
        python complex_example.py --serial_size 100000 --thread_size 0 --process_size 0
    """
    main()
