from __future__ import annotations

import atexit
import os
import pickle
import shutil
from argparse import ArgumentParser
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import partial
from itertools import count
from multiprocessing import dummy, get_context
from multiprocessing.process import BaseProcess
from pathlib import Path
from tempfile import mkdtemp, mkstemp, TemporaryDirectory
from time import sleep
from typing import Any, ClassVar, Generic, Literal, TypeVar, cast, final
from typing_extensions import ParamSpec, Self

from external_module import my_external_sum, split_workload


NUM_NUMBERS = 100
NUM_PROCS = 4

T = TypeVar('T')
PS = ParamSpec('PS')
StartMethod = Literal['fork', 'forkserver', 'spawn', 'dummy']


def my_local_sum(x: list[int], fail: bool = False) -> int:
    result: int = 0  # GREP_MARKER[LOCAL-INVOCATION]
    for item in x:
        result += item  # GREP_MARKER[LOCAL-LOOP]
    if fail:
        raise RuntimeError('forced failure')
    return result


def _format_attrs(attrs: Mapping[str, Any]) -> str:
    return ', '.join(f'{attr}={value!r}' for attr, value in attrs.items())


@dataclass(init=False)
class Timeout(RuntimeError):
    worker: Worker
    timeout: float

    def __init__(self, worker: Worker, timeout: float) -> None:
        super().__init__(worker, timeout)
        self.worker = worker
        self.timeout = timeout

    def _get_repr_attrs(self) -> dict[str, Any]:
        return {**self.worker._get_repr_attrs(), 'timeout': self.timeout}

    def __str__(self) -> str:
        return 'timed out in {:.2g} s ({})'.format(
            self.timeout, _format_attrs(self.worker._get_repr_attrs()),
        )

    def __repr__(self) -> str:
        attrs = _format_attrs({
            'timeout': self.timeout, **self.worker._get_repr_attrs(),
        })
        return '<{} @ {:#x} ({})>'.format(type(self).__name__, id(self), attrs)


@final
@dataclass
class Worker(Generic[T]):
    """
    Example:
        >>> import os
        >>> from time import sleep

        >>> with Worker.new(sum, args=(range(10),)) as worker:
        ...     assert os.getpid() != worker.process.pid is not None
        ...     assert worker.get_result() == 45
        >>> with Worker.new(sleep, args=(10,), daemon=True) as worker:
        ...     worker.get_result(timeout=.125)  # doctest: +ELLIPSIS
        Traceback (most recent call last):
          ...
        process_test_module.Timeout: timed out in 0.12 s (...)
    """
    process: BaseProcess
    tmpdir: Path
    result_callback: Callable[[], T]
    _result: T = field(init=False)
    _counter: ClassVar[count] = count()

    def get_result(self, timeout: float | None = None) -> T:
        try:
            return self._result
        except AttributeError:
            pass
        self.process.join(timeout)
        if self.process.is_alive():
            assert timeout is not None
            raise Timeout(self, timeout)
        self._result = self.result_callback()
        return self._result

    def _get_repr_attrs(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            'name': self.process.name,
            'pid': self.process.pid,
            'exitcode': self.process.exitcode,
        }
        try:
            attrs['result'] = self._result
        except AttributeError:
            pass
        return attrs

    def __repr__(self) -> str:
        attrs = _format_attrs(self._get_repr_attrs())
        return '<{} @ {:#x} ({})>'.format(type(self).__name__, id(self), attrs)

    def __enter__(self) -> Self:
        self.process.start()
        return self

    def __exit__(self, *_, **__) -> None:
        try:
            self._end_process('terminate')
        finally:
            self._end_process('join')
            self._end_process('close')

    def _end_process(
        self, method: Literal['terminate', 'kill', 'join', 'close'], /,
        *args, **kwargs
    ) -> None:
        if self.process.is_alive():
            getattr(self.process, method)(*args, **kwargs)
            sleep(0)

    @classmethod
    def new(
        cls,
        target: Callable[..., T],
        *,
        args: Sequence[Any] | None = None,
        kwargs: Mapping[str, Any] | None = None,
        start_method: StartMethod | None = None,
        tmpdir: Path | None = None,
        **kw
    ) -> Worker[T]:
        if tmpdir is None:
            tmpdir = Path(mkdtemp(prefix='worker-tmpdir-'))
            rmdir = partial(shutil.rmtree, tmpdir, ignore_errors=True)
            atexit.register(rmdir)
        get_process: Callable[..., BaseProcess]
        if start_method == 'dummy':
            # Type-checkers don't seem to like a subclass as a
            # base-class constructor...
            get_process = cast(Callable[..., BaseProcess], dummy.Process)
        else:
            get_process = get_context(start_method).Process
        handle, fname = mkstemp(suffix='.pkl', dir=tmpdir)
        outfile = Path(fname)
        os.close(handle)
        proc = get_process(
            target=cls._write_result,
            args=(outfile, target, *(args or ())),
            kwargs=(kwargs or {}),
            name=f'MyWorker-{cls._get_count()}',
            **kw,
        )
        result_callback = cast(
            Callable[[], T], partial(cls._read_result, outfile),
        )
        return cls(proc, tmpdir, result_callback)

    @classmethod
    def _get_count(cls) -> int:
        return next(cls._counter)

    @staticmethod
    def _read_result(out: Path) -> Any:
        with out.open(mode='rb') as fobj:
            ok, result = pickle.load(fobj)
        if ok:
            return result
        else:
            assert isinstance(result, BaseException)
            raise result from None

    @staticmethod
    def _write_result(
        out: Path, func: Callable[PS, Any], /,
        *args: PS.args, **kwargs: PS.kwargs
    ) -> None:
        try:
            result = True, func(*args, **kwargs)
        except BaseException as e:
            result = False, type(e)(*e.args)  # Truncate traceback
            raise
        finally:
            with out.open(mode='wb') as fobj:
                pickle.dump(result, fobj)


def gather_results(
    workers: Sequence[Worker[T]], timeout: float | None = None,
) -> list[T]:
    """
    Attempt to gather all partial results from all workers even if some
    workers errored out.

    Note:
        The ``timeout`` is applied on a per-worker basis.
    """
    result: list[T] = []
    xc: BaseException | None = None
    for worker in workers:
        try:
            result.append(worker.get_result(timeout))
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
    with ExitStack() as stack:
        tmpdir = Path(stack.enter_context(TemporaryDirectory()))
        new_worker = partial(
            Worker.new, start_method=start_method, tmpdir=tmpdir, daemon=True,
        )
        workers: list[Worker] = []
        for entries in split_workload(length, n):
            worker = new_worker(my_sum, args=(entries, fail))
            workers.append(stack.enter_context(worker))
        return my_sum(gather_results(workers, timeout=timeout), fail)


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
