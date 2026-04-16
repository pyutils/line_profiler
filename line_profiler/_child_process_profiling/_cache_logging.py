"""
Logging utilities.
"""
from __future__ import annotations

import os
import re
from collections.abc import Generator
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from string import Formatter as StringParser
from textwrap import dedent
from typing import TYPE_CHECKING, NamedTuple, TextIO, overload
from typing_extensions import Self

from .. import _diagnostics as diagnostics
from .misc_utils import block_indent


__all__ = ('CacheLoggingEntry',)


FILENAME_PATTERN = 'debug_log_{main_pid}_{current_pid}.log'
TIMESTAMP_PATTERN = '[cache-debug-log {timestamp} DEBUG]'
HEADER_PATTERN = 'PID {current_pid} ({main_pid}): Cache {obj_id:#x}'

TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'
TIMESTAMP_MICROSECOND_SEP = ','
TIMESTAMP_MICROSECOND_PLACES = 3
TIMESTAMP_SPACING = ' '

HEADER_SEP = ': '
HEADER_MAIN_INDICATOR = 'main process'


def get_logger_header(current_pid: int, main_pid: int, obj_id: int) -> str:
    """
    Returns:
        msg_header (str):
            Message header, to be prefixed to messages sent to
            :py:data:`line_profiler._diagnostics.log`.
    """
    return HEADER_PATTERN.format(
        current_pid=current_pid,
        main_pid=(
            HEADER_MAIN_INDICATOR if main_pid == current_pid else main_pid
        ),
        obj_id=obj_id,
    )


def format_timestamp(ts: datetime) -> str:
    """
    Replicate the :py:mod:`logging`'s default formatting for timestamps.

    Example:
        >>> ts = datetime(2000, 1, 23, 4, 5, 6, 789000)
        >>> as_str = format_timestamp(ts)
        >>> print(as_str)
        2000-01-23 04:05:06,789
        >>> assert parse_timestamp(as_str) == ts
    """
    return '{}{}{:0{}d}'.format(
        ts.strftime(TIMESTAMP_FORMAT),
        TIMESTAMP_MICROSECOND_SEP,
        int(ts.microsecond / 1000),
        TIMESTAMP_MICROSECOND_PLACES,
    )


def parse_timestamp(ts: str) -> datetime:
    """
    Turn a formatted string timestamp back to a
    :py:class:`datetime.datetime` object.
    """
    assert TIMESTAMP_MICROSECOND_SEP in ts
    base, _, fractional = ts.rpartition(TIMESTAMP_MICROSECOND_SEP)
    # The microsecond field %f must be 6 digits long
    if len(fractional) < 6:
        fractional = f'{fractional:<06}'
    else:
        fractional = fractional[:6]
    parse_format = f'{TIMESTAMP_FORMAT}{TIMESTAMP_MICROSECOND_SEP}%f'
    ts = f'{base}{TIMESTAMP_MICROSECOND_SEP}{fractional}'
    return datetime.strptime(ts, parse_format)


def add_timestamp(msg: str, timestamp: datetime | None = None) -> str:
    """
    Returns:
        msg_with_timestamp (str):
            (Block-indented) message with timestamp, to be written to
            the :py:attr:`LineProfilingCache._debug_log`.
    """
    if timestamp is None:
        timestamp = datetime.now()
    ts_formatted = TIMESTAMP_PATTERN.format(
        timestamp=format_timestamp(timestamp),
    )
    return block_indent(msg, ts_formatted + TIMESTAMP_SPACING)


def parse_id(uint: str) -> int:
    """
    Example:
        >>> n = 123456
        >>> for formatter in str, bin, oct, hex:
        ...     assert parse_id(formatter(n)) == n
    """
    for prefix, base in ('0b', 2), ('0o', 8), ('0x', 16):
        if uint.startswith(prefix):
            return int(uint[len(prefix):], base=base)
    return int(uint)


@overload
def fmt_to_regex(fmt: str, /, *auto_numbered_fields: str) -> str:
    ...


@overload
def fmt_to_regex(fmt: str, /, **named_fields: str) -> str:
    ...


def fmt_to_regex(
    fmt: str, /, *auto_numbered_fields: str, **named_fields: str
) -> str:
    """
    Example:
        >>> import re

        Simple case:

        >>> pattern = fmt_to_regex(
        ...     '{func}({args})', func=r'[_\\w][_\\w\\d]+', args='.*',
        ... )
        >>> print(pattern)
        (?P<func>[_\\w][_\\w\\d]+)\\((?P<args>.*)\\)
        >>> regex = re.compile('^' + pattern, re.MULTILINE)
        >>> assert not regex.search('0(1)')
        >>> match = regex.search('    \\nint(-1.5)')
        >>> assert match.group('func', 'args') == ('int', '-1.5')

        Repeated fields:

        >>> palindrome_5l = re.compile(fmt_to_regex(
        ...     '{first}{second}{third}{second}{first}',
        ...     first='.', second='.', third='.',
        ... ))
        >>> print(palindrome_5l.pattern)
        (?P<first>.)(?P<second>.)(?P<third>.)(?P=second)(?P=first)
        >>> assert not palindrome_5l.match('abbbe')
        >>> match = palindrome_5l.match('aBcBa')
        >>> assert match.group('first', 'second', 'third') == (
        ...     'a', 'B', 'c',
        ... )

        Auto-numbered fields:

        >>> print(fmt_to_regex(
        ...     '[{} {}-{}-{} {}:{}:{},{} {}]',
        ...     # Logger name
        ...     '.+',
        ...     # Date
        ...     r'\\d\\d', r'\\d\\d', r'\\d\\d',
        ...     # Time + milliseconds
        ...     r'\\d\\d', r'\\d\\d', r'\\d\\d', r'\\d\\d\\d',
        ...     # Category
        ...     'DEBUG|INFO|WARNING|ERROR|CRITICAL',
        ... ))
        \\[(.+)\\ (\\d\\d)\\-(\\d\\d)\\-(\\d\\d)\\ \
(\\d\\d):(\\d\\d):(\\d\\d),(\\d\\d\\d)\\ \
(DEBUG|INFO|WARNING|ERROR|CRITICAL)\\]
    """
    chunks: list[str] = []
    seen_fields: set[str] = set()
    for i, (prefix, field, *_) in enumerate(StringParser().parse(fmt)):
        chunks.append(re.escape(prefix))
        if field is None:
            break  # Suffix -> we're done
        if field:  # Named fields
            assert field.isidentifier()
            if field in seen_fields:
                chunks.append(f'(?P={field})')
            else:
                chunks.append(f'(?P<{field}>{named_fields[field]})')
                seen_fields.add(field)
        else:  # Auto-numbered fields
            chunks.append(f'({auto_numbered_fields[i]})')
    return ''.join(chunks)


class CacheLoggingEntry(NamedTuple):
    """
    Logging entry written to a log file by
    :py:meth:`LineProfilingCache._debug_output`.

    Example:
        >>> from datetime import datetime
        >>>
        >>>
        >>> entry = CacheLoggingEntry(
        ...     datetime(1900, 1, 1, 0, 0, 0, 0),
        ...     12345,
        ...     12345,
        ...     12345678,
        ...     'This is a log message;\\nit has multiple lines',
        ... )
        >>> print(entry.to_text())
        [cache-debug-log 1900-01-01 00:00:00,000 DEBUG] PID 12345 \
(main process): Cache 0xbc614e: This is a log message;
                                                        it has \
multiple lines
        >>> another_entry = CacheLoggingEntry(
        ...     datetime(2000, 12, 31, 12, 34, 56, 789000),
        ...     12345,
        ...     54321,
        ...     87654321,
        ...     'FOO BAR BAZ',
        ... )
        >>> print(another_entry.to_text())
        [cache-debug-log 2000-12-31 12:34:56,789 DEBUG] PID 54321 \
(12345): Cache 0x5397fb1: FOO BAR BAZ
        >>> log_text = '\\n'.join([
        ...     e.to_text() for e in [entry, another_entry]
        ... ])
        >>> assert CacheLoggingEntry.from_text(log_text) == [
        ...     entry, another_entry,
        ... ]
    """
    timestamp: datetime
    main_pid: int
    current_pid: int
    cache_id: int
    msg: str

    def to_text(self) -> str:
        return add_timestamp(self._get_header() + self.msg, self.timestamp)

    def _get_header(self) -> str:
        return get_logger_header(
            self.current_pid, self.main_pid, self.cache_id,
        ) + HEADER_SEP

    def write(self, tee: os.PathLike[str] | str | None = None) -> None:
        log_msg = self._get_header() + self.msg
        diagnostics.log.debug(log_msg)
        if tee is None:
            return
        with Path(tee).open(mode='a') as fobj:
            print(add_timestamp(log_msg, self.timestamp), file=fobj)

    @classmethod
    def new(cls, main_pid: int, cache_id: int, msg: str) -> Self:
        return cls(datetime.now(), main_pid, os.getpid(), cache_id, msg)

    @classmethod
    def from_file(cls, file: os.PathLike[str] | str | TextIO) -> list[Self]:
        try:
            path = Path(file)  # type: ignore
        except TypeError:  # File object
            # If we're here, `file` is a file object
            if TYPE_CHECKING:
                assert isinstance(file, TextIO)
            content = file.read()
        else:
            content = path.read_text()
        return cls.from_text(content)

    @classmethod
    def from_text(cls, text: str) -> list[Self]:
        def gen_timestamps(text: str) -> Generator[re.Match, None, None]:
            last_ts_match: re.Match | None = None
            while True:
                ts_match = timestamp_regex.search(
                    text, last_ts_match.end() if last_ts_match else 0,
                )
                if ts_match:
                    yield ts_match
                    last_ts_match = ts_match
                else:
                    return

        def gen_message_blocks(text: str) -> Generator[
            tuple[datetime, re.Match, str], None, None
        ]:
            timestamps = list(gen_timestamps(text))
            if not timestamps:
                return

            # Handle all the entries up till the 2nd-to-last one
            for this_match, next_match in pairwise(timestamps):
                ts = parse_timestamp(this_match.group('timestamp'))
                # The -1 is for stripping the trailing newline
                text_block = text[this_match.start():next_match.start() - 1]
                yield (ts, this_match, text_block)
            # Handle the last entry
            last_match = timestamps[-1]
            yield (
                parse_timestamp(last_match.group('timestamp')),
                last_match,
                text[last_match.start():],
            )

        def get_entries(text: str) -> Generator[Self, None, None]:
            for timestamp, ts_match, text_block in gen_message_blocks(text):
                # Strip the block indent
                ts_text = ts_match.group(0)
                assert text_block.startswith(ts_text)
                ts_width = len(ts_text)
                text_block = dedent(' ' * ts_width + text_block[ts_width:])
                # Strip the header and parse the relevant info from it
                header_match = header_regex.match(text_block)
                assert header_match
                current_pid = int(header_match.group('current_pid'))
                main_pid_ = header_match.group('main_pid')
                if main_pid_ == HEADER_MAIN_INDICATOR:
                    main_pid = current_pid
                else:
                    main_pid = int(main_pid_)
                cache_id = parse_id(header_match.group('obj_id'))
                # The rest of the block is the message proper
                msg = text_block[header_match.end():]
                yield cls(timestamp, main_pid, current_pid, cache_id, msg)

        timestamp_pattern = fmt_to_regex(
            f'{TIMESTAMP_PATTERN}{TIMESTAMP_SPACING}', timestamp='.+?',
        )
        timestamp_regex = re.compile('^' + timestamp_pattern, re.MULTILINE)
        header_regex = re.compile(fmt_to_regex(
            HEADER_PATTERN + HEADER_SEP,
            current_pid=r'\d+',
            main_pid=r'\d+|' + re.escape(HEADER_MAIN_INDICATOR),
            obj_id='.+?',
        ))
        return list(get_entries(text))
