"""
A script used in test_complex_case.py
"""
import line_profiler
import atexit


profile = line_profiler.LineProfiler()


@atexit.register
def _show_profile_on_end():
    profile.print_stats()


@profile
def fib(n):
    a, b = 0, 1
    while a < n:
        print(a, end=' ')
        a, b = b, a + b
    print()


@profile
def funcy_fib(n):
    """
    Alternatite fib function where code splits out over multiple lines
    """
    a, b = (
        0, 1
    )
    while a < n:
        print(
            a, end=' ')
        a, b = b, \
                a + b
    print(
    )


@profile
def fib_only_called_by_thread(n):
    a, b = 0, 1
    while a < n:
        print(a, end=' ')
        a, b = b, a + b
    print()


@profile
def fib_only_called_by_process(n):
    a, b = 0, 1
    while a < n:
        print(a, end=' ')
        a, b = b, a + b
    # FIXME: having two functions with the EXACT same code can cause issues
    # a = 'no longer exactly the same'
    print()


@profile
def main():
    """
    Run a lot of different Fibonacci jobs
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--size', type=int, default=10)
    args = parser.parse_args()

    size = args.size

    for i in range(size):
        fib(i)
        funcy_fib(
            i)
        fib(i)

    from concurrent.futures import ThreadPoolExecutor
    executor = ThreadPoolExecutor(max_workers=4)
    with executor:
        jobs = []
        for i in range(size):
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
        for i in range(size):
            job = executor.submit(fib, i)
            jobs.append(job)

            job = executor.submit(funcy_fib, i)
            jobs.append(job)

            job = executor.submit(fib_only_called_by_process, i)
            jobs.append(job)

        for job in jobs:
            job.result()


if __name__ == '__main__':
    """
    CommandLine:
        cd ~/code/line_profiler/tests/
        python complex_example.py --size 10
    """
    main()
