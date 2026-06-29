from __future__ import annotations


def my_external_sum(x: list[int], fail: bool = False) -> int:
    result: int = 0  # GREP_MARKER[EXT-INVOCATION]
    for item in x:
        result += item  # GREP_MARKER[EXT-LOOP]
    if fail:
        raise RuntimeError('forced failure')
    return result
