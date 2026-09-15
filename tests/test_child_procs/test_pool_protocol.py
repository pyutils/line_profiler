"""
Tests for the tagged pool result protocol.

Patched pool workers push ``(tag, pid, result)`` triplets so th patched
parent can e.g. attribute results to worker PIDs.  A worker that was
never patched (e.g. its interpreter never loaded the profiling startup
hook, or it runs via ``multiprocessing.set_executable()`` pointing at a
different Python) pushes vanilla results; the parent must pass those
through with a warning rather than crash its result-handler thread —
the crash presents to the user as ``pool.map()`` hanging forever.
"""
import math
import multiprocessing
import warnings
from collections.abc import Callable
from typing import Any, Literal
from typing_extensions import Never

import pytest

from line_profiler._child_process_profiling.cache import LineProfilingCache
from line_profiler._child_process_profiling.multiprocessing_patches import (
    _queue, apply as mp_apply,
)

from ._test_child_procs_utils import CheckWarnings


GET_TIMEOUT = 60
TEST_TAG = '__test_pool_protocol_test_tag__'


def test_put_wrapper_tags_results() -> None:
    """
    Check that :py:class:`line_profiler._child_process_profiling.\
multiprocessing_patches._queue.PutWrapper`
    behaves as expected, inserting a tag and the return value of a
    callback into the pushed tuple.
    """
    pushed: list[Any] = []

    class FakeQueue:
        @staticmethod
        def put(obj: Any) -> None:
            pushed.append(obj)

        @staticmethod
        def get() -> Never:  # Just here to comply with the interface
            raise NotImplementedError

    wrapper = _queue.PutWrapper(FakeQueue(), lambda: 1234, tag=TEST_TAG)
    wrapper.put(('job', 0, 'result'))
    assert pushed == [(TEST_TAG, 1234, ('job', 0, 'result'))]


@pytest.mark.parametrize(
    'incoming, expected_result, expected_data, is_tagged, is_sentinel',
    [
        (
            (TEST_TAG, 1234, ('job', 0, 'ok')),
            ('job', 0, 'ok'), 1234, True, False,
        ),
        # vanilla worker
        (('job', 0, 'ok'), ('job', 0, 'ok'), None, False, False),
        (None, None, None, True, True),  # queue sentinel, never warns
    ],
)
def test_quick_get_wrapper_unwrapping_results(
    create_cache: Callable[..., LineProfilingCache],
    incoming: tuple[Any, ...] | None,
    expected_result: tuple[Any, ...] | None,
    expected_data: Any,
    is_tagged: bool,
    is_sentinel: bool,
) -> None:
    """
    Check that :py:class:`line_profiler._child_process_profiling.\
multiprocessing_patches._queue.QuickGetWrapper`
    behaves as expected, pulling a tag and the extra data from the
    pulled tuple (where appropriate) and returning the original result.
    """
    def get() -> tuple[Any, ...] | None:
        return incoming

    def store(_, data: int) -> None:
        stored.append(data)

    cache = create_cache(_use_curated_profiler=False)
    stored: list[Any] = []
    quick_get = _queue.QuickGetWrapper(cache, get, store, TEST_TAG)
    checked_warning = {
        'category': UserWarning,
        'message': f'.*without the expected tag.*{TEST_TAG}',
    }
    if is_tagged:
        with CheckWarnings() as cw:
            cw.forbid_warnings(**checked_warning)
            assert quick_get() == expected_result
    else:
        with CheckWarnings(reissue_warnings=False) as cw:
            cw.expect_warnings(**checked_warning)
            assert quick_get() == expected_result
        # ... but only once per session
        with CheckWarnings() as cw:
            cw.forbid_warnings(**checked_warning)
            assert quick_get() == expected_result
    if is_tagged and not is_sentinel:
        assert stored == [expected_data]
    else:
        assert not stored


def _run_pool_map(
    method: Literal['spawn', 'fork', 'forkserver'],
) -> list[float]:
    import os

    ctx = multiprocessing.get_context(method)
    with ctx.Pool(1) as pool:
        result = pool.map_async(math.sqrt, [0, 1, 4, 9])
        # A hard timeout so that a protocol regression fails fast
        # instead of hanging the suite (the historical failure mode)
        values = result.get(timeout=GET_TIMEOUT)
        # Guard against environments where the pool silently degrades:
        # the values must really come from another process
        worker_pid = pool.apply_async(os.getpid).get(timeout=GET_TIMEOUT)
    assert worker_pid != os.getpid()
    return values


def test_patched_parent_with_vanilla_spawn_worker(
    create_cache: Callable[..., LineProfilingCache],
) -> None:
    """
    The parent is patched, but the spawn children know nothing of the
    profiling session (no env vars are injected, no .pth hook exists
    for them), so they push vanilla un-tagged results: the map must
    still complete, with a warning.
    """
    if 'spawn' not in multiprocessing.get_all_start_methods():
        pytest.skip("start method 'spawn' unavailable")
    cache = create_cache(_use_curated_profiler=False)
    # Make the wrapped methods resolve `LineProfilingCache.load()` to
    # this instance without injecting env vars (children stay vanilla)
    cache._replace_loaded_instance(force=True)
    mp_apply(cache)
    with pytest.warns(UserWarning, match='without the expected'):
        assert _run_pool_map('spawn') == [0.0, 1.0, 2.0, 3.0]


def test_patched_parent_with_patched_fork_worker(
    create_cache: Callable[..., LineProfilingCache],
) -> None:
    """
    Control case: fork children inherit the parent's patches, results
    arrive tagged, and no warning fires.
    """
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip("start method 'fork' unavailable")
    cache = create_cache()
    cache._replace_loaded_instance(force=True)
    mp_apply(cache)
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        assert _run_pool_map('fork') == [0.0, 1.0, 2.0, 3.0]
