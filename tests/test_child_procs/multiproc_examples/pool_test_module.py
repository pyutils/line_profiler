from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Callable
from multiprocessing import dummy, get_context, Pool
from typing import Literal

from external_module import my_external_sum


NUM_NUMBERS = 100
NUM_PROCS = 4


def my_local_sum(x: list[int], fail: bool = False) -> int:
    result: int = 0  # GREP_MARKER[LOCAL-INVOCATION]
    # The reversing is to prevent bytecode aliasing with
    # `my_external_sum()` (see issue #424, PR #425)
    for item in reversed(x):
        result += item  # GREP_MARKER[LOCAL-LOOP]
    if fail:
        raise RuntimeError('forced failure')
    return result


def sum_in_child_procs(
    length: int, n: int, my_sum: Callable[[list[int], bool], int],
    start_method: Literal[
        'fork', 'forkserver', 'spawn', 'dummy'
    ] | None = None,
    fail: bool = False,
) -> int:
    my_list: list[int] = list(range(1, length + 1))
    sublists: list[list[int]] = []
    subsums: list[int]
    sublength = length // n
    if sublength * n < length:
        sublength += 1
    while my_list:
        sublist, my_list = my_list[:sublength], my_list[sublength:]
        sublists.append(sublist)
    if start_method == 'dummy':
        pool = dummy.Pool(n)
    elif start_method:
        pool = get_context(start_method).Pool(n)
    else:
        pool = Pool(n)
    with pool:
        subsums = pool.starmap(my_sum, [(sl, fail) for sl in sublists])
        pool.close()
        pool.join()
    return my_sum(subsums, fail)


def main(args: list[str] | None = None) -> None:
    parser = ArgumentParser()
    parser.add_argument('-l', '--length', type=int, default=NUM_NUMBERS)
    parser.add_argument('-n', type=int, default=NUM_PROCS)
    parser.add_argument(
        '-s', '--start-method',
        choices=['fork', 'forkserver', 'spawn'], default=None,
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
