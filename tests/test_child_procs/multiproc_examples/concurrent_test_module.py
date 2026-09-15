from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Callable, Sequence
from concurrent.futures import (
    Executor, Future, ThreadPoolExecutor, ProcessPoolExecutor,
)
from multiprocessing import get_context
from typing import Literal, TypeVar

from external_module import my_external_sum
from external_module import split_workload  # See issue #433


NUM_NUMBERS = 100
NUM_PROCS = 4

T = TypeVar('T')
StartMethod = Literal['fork', 'forkserver', 'spawn', 'dummy']


def my_local_sum(x: list[int], fail: bool = False) -> int:
    result: int = 0  # GREP_MARKER[LOCAL-INVOCATION]
    for item in x:
        result += item  # GREP_MARKER[LOCAL-LOOP]
    if fail:
        raise RuntimeError('forced failure')
    return result


def gather_results(
    futures: Sequence[Future[T]], timeout: float | None = None,
) -> list[T]:
    """
    Attempt to gather all partial results from all futures even if some
    futures errored out.

    Note:
        - The ``timeout`` is applied on a per-future basis.

        - We do this instead of :py:meth:`.Executor.map` because the
          latter results in unrun tasks being cancelled when earlier
          ones error out.
    """
    result: list[T] = []
    xc: BaseException | None = None
    for future in futures:
        try:
            result.append(future.result(timeout))
        except BaseException as e:
            xc = e
    if xc is None:
        return result
    raise xc


def sum_in_child_procs(
    length: int, n: int, my_sum: Callable[[list[int], bool], int],
    start_method: StartMethod | None = None,
    fail: bool = False,
    timeout: float | None = None,
) -> int:
    if start_method == 'dummy':
        executor: Executor = ThreadPoolExecutor(n)
    else:
        ctx = get_context(start_method)
        executor = ProcessPoolExecutor(n, mp_context=ctx)
    with executor:
        futures = [
            executor.submit(my_sum, workload, fail)
            for workload in split_workload(length, n)
        ]
        return my_sum(gather_results(futures, timeout=timeout), fail)


def main(args: list[str] | None = None) -> None:
    parser = ArgumentParser()
    parser.add_argument('-l', '--length', type=int, default=NUM_NUMBERS)
    parser.add_argument('-n', type=int, default=NUM_PROCS)
    parser.add_argument(
        '-s', '--start-method',
        choices=['fork', 'forkserver', 'spawn', 'dummy'], default=None,
    )
    parser.add_argument('-f', '--force-failure', action='store_true')
    parser.add_argument(
        '--local',
        action='store_const',
        dest='my_sum',
        default=my_external_sum,
        const=my_local_sum,
    )
    options = parser.parse_args(args)
    print(sum_in_child_procs(
        options.length, options.n, options.my_sum,
        start_method=options.start_method,
        fail=options.force_failure,
    ))


if __name__ == '__main__':
    main()
