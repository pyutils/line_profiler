from __future__ import annotations

from itertools import islice


def my_external_sum(x: list[int], fail: bool = False) -> int:
    result: int = 0  # GREP_MARKER[EXT-INVOCATION]
    for item in x:
        result += item  # GREP_MARKER[EXT-LOOP]
    if fail:
        raise RuntimeError('forced failure')
    return result


def split_workload(length: int, n: int) -> list[list[int]]:
    """
    Example:
        >>> split_workload(10, 3)
        [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10]]
        >>> split_workload(8, 4)
        [[1, 2], [3, 4], [5, 6], [7, 8]]
    """
    iter_entries = iter(range(1, length + 1))
    result: list[list[int]] = []
    sublength = length // n
    if sublength * n < length:
        sublength += 1
    while True:
        sublist = list(islice(iter_entries, sublength))
        if not sublist:
            break
        result.append(sublist)
    return result
