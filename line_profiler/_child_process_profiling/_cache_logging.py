"""
Logging utilities.
"""
from __future__ import annotations

import os
import re
from collections.abc import Generator
from datetime import datetime
from enum import auto
from itertools import pairwise
from pathlib import Path
from string import Formatter as StringParser
from typing import TYPE_CHECKING, NamedTuple, TextIO, overload
from typing_extensions import Self
from warnings import warn

from .. import _diagnostics as diagnostics
from ..line_profiler_utils import block_indent, StringEnum


__all__ = ('CacheLoggingEntry',)

FILENAME_PATTERN = 'debug_log_{main_pid}_{current_pid}.log'
TIMESTAMP_PATTERN = '[cache-debug-log {timestamp} {level}]'
HEADER_PATTERN = 'PID {current_pid} ({main_pid}): Cache {obj_id:#x}'

TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'
TIMESTAMP_MICROSECOND_SEP = ','
TIMESTAMP_MICROSECOND_PLACES = 3
TIMESTAMP_SPACING = ' '

HEADER_SEP = ': '
HEADER_MAIN_INDICATOR = 'main process'


class LogLevel(StringEnum):
    DEBUG = auto()
    INFO = auto()
    WARNING = auto()
    ERROR = auto()
    CRITICAL = auto()


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
    assert TIMESTAMP_MICROSECOND_SEP in ts, (
        f'{ts=!r}, {TIMESTAMP_MICROSECOND_SEP=!r}'
    )
    base, _, fractional = ts.rpartition(TIMESTAMP_MICROSECOND_SEP)
    # The microsecond field %f must be 6 digits long
    if len(fractional) < 6:
        fractional = f'{fractional:<06}'
    else:
        fractional = fractional[:6]
    parse_format = f'{TIMESTAMP_FORMAT}{TIMESTAMP_MICROSECOND_SEP}%f'
    ts = f'{base}{TIMESTAMP_MICROSECOND_SEP}{fractional}'
    return datetime.strptime(ts, parse_format)


def add_timestamp(
    msg: str,
    timestamp: datetime | None = None,
    level: str | LogLevel = LogLevel.DEBUG,
) -> str:
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
        level=str(level).upper(),
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
            assert field.isidentifier(), f'{field=!r}'
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
        ...     LogLevel.DEBUG,
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
        ...     LogLevel.INFO,
        ...     12345,
        ...     54321,
        ...     87654321,
        ...     'FOO BAR BAZ',
        ... )
        >>> print(another_entry.to_text())
        [cache-debug-log 2000-12-31 12:34:56,789 INFO] PID 54321 \
(12345): Cache 0x5397fb1: FOO BAR BAZ
        >>> log_text = '\\n'.join([
        ...     e.to_text() for e in [entry, another_entry]
        ... ])
        >>> assert CacheLoggingEntry.from_text(log_text) == [
        ...     entry, another_entry,
        ... ]
    """
    timestamp: datetime
    level: LogLevel
    main_pid: int
    current_pid: int
    cache_id: int
    msg: str

    def to_text(self) -> str:
        return add_timestamp(
            self._get_header() + self.msg, self.timestamp, self.level,
        )

    def _get_header(self) -> str:
        return get_logger_header(
            self.current_pid, self.main_pid, self.cache_id,
        ) + HEADER_SEP

    def write(self, tee: os.PathLike[str] | str | None = None) -> None:
        """
        Write the log message using
        :py:mod:`line_profiler._diagnostics.log`. If ``tee`` is a path,
        also tee thereto with an appropriate timestamp.
        """
        log_msg = self._get_header() + self.msg
        log_func = getattr(diagnostics.log, self.level.lower())
        log_func(log_msg)
        if tee is None:
            return
        with Path(tee).open(mode='a') as fobj:
            full_msg = add_timestamp(log_msg, self.timestamp, self.level)
            print(full_msg, file=fobj)

    @classmethod
    def new(
        cls, main_pid: int, cache_id: int, msg: str,
        level: str | LogLevel = LogLevel.DEBUG,
    ) -> Self:
        return cls(
            datetime.now(), LogLevel(level), main_pid,
            os.getpid(), cache_id, msg,
        )

    @classmethod
    def from_file(cls, file: os.PathLike[str] | str | TextIO) -> list[Self]:
        try:
            path = Path(file)  # type: ignore
        except TypeError:  # File object
            # If we're here, `file` is a file object
            if TYPE_CHECKING:
                assert isinstance(file, TextIO), f'{file=!r}'
            content = file.read()
        else:
            content = path.read_text()
        return cls.from_text(content)

    @classmethod
    def from_text(cls, text: str) -> list[Self]:
        return list(cls._gen_entries_from_text(text))

    @staticmethod
    def _gen_timestamps_from_text(
        text: str,
    ) -> Generator[re.Match, None, None]:
        timestamp_pattern = fmt_to_regex(
            f'{TIMESTAMP_PATTERN}{TIMESTAMP_SPACING}',
            timestamp='.+?',
            level='|'.join(LogLevel.__members__),
        )
        timestamp_regex = re.compile('^' + timestamp_pattern, re.MULTILINE)
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

    @classmethod
    def _gen_message_blocks_from_text(
        cls, text: str,
    ) -> Generator[tuple[re.Match, str], None, None]:
        timestamps = list(cls._gen_timestamps_from_text(text))
        if not timestamps:
            return
        # Handle all the entries up till the 2nd-to-last one
        for this_match, next_match in pairwise(timestamps):
            text_block = text[this_match.start():next_match.start()]
            yield (this_match, text_block.rstrip('\n'))
        # Handle the last entry
        last_match = timestamps[-1]
        yield (last_match, text[last_match.start():].rstrip('\n'))

    @classmethod
    def _gen_entries_from_text(cls, text: str) -> Generator[Self, None, None]:
        r"""
        Example:
            >>> import re
            >>> from contextlib import AbstractContextManager, ExitStack
            >>> from functools import partial
            >>> from warnings import WarningMessage, catch_warnings

            >>> from line_profiler import _diagnostics as diag
            >>> from line_profiler.line_profiler_utils import restore

            >>> class record_warnings:
            ...     def __init__(self) -> None:
            ...         self._stacks = []
            ...
            ...     def __enter__(self) -> list[WarningMessage]:
            ...         stack = ExitStack()
            ...         enter = stack.enter_context
            ...         self._stacks.append(stack)
            ...         warnings = enter(catch_warnings(record=True))
            ...         # Suppress logger output
            ...         enter(restore.instance_dict(
            ...             diag.log, attrs=['_backend'],
            ...         ))
            ...         diag.log.configure(verbose=0)
            ...         return warnings
            ...
            ...     def __exit__(self, *_, **__) -> None:
            ...         self._stacks.pop().close()

            >>> def get_entry(
            ...     msg: str, **kwargs
            ... ) -> CacheLoggingEntry:
            ...     kwargs.setdefault('main_pid', 1234)
            ...     kwargs.setdefault('cache_id', 0x12345678)
            ...     entry = CacheLoggingEntry.new(msg=msg, **kwargs)
            ...     # Note: we only get 3 subsecond digits in the
            ...     # `.to_text()` output, so round the timestamp
            ...     # accordingly
            ...     milliseconds = entry.timestamp.microsecond // 1000
            ...     adjusted_ts = entry.timestamp.replace(
            ...         microsecond=milliseconds * 1000,
            ...     )
            ...     return entry._replace(timestamp=adjusted_ts)

            >>> get_entries = CacheLoggingEntry._gen_entries_from_text
            >>> e1 = get_entry('foo bar\n baz')
            >>> e2 = get_entry('this\nwill\nbe\n truncated')
            >>> e3 = get_entry('another normal\n   entry')
            >>> all_entries = [e1, e2, e3]

            Log-entry corruption:

            >>> with record_warnings() as warnings:
            ...     log = [
            ...         e1.to_text(),
            ...         e2.to_text().split('Cache')[0],  # No cache ID
            ...         e3.to_text()
            ...     ]
            ...     parsed = list(get_entries('\n'.join(log)))

            >>> assert (
            ...     parsed == [e1, e3]
            ... ), f'{all_entries=!r}, {parsed=!r}'
            >>> assert len(warnings) == 1
            >>> assert re.search(
            ...     'failed to parse .* after parsing 1 entry/-ies',
            ...     str(warnings[0].message),
            ... ), warnings

            Timestamp truncation:

            >>> with record_warnings() as warnings:
            ...     log = [
            ...         e1.to_text(),
            ...         e2.to_text().split('DEBUG')[0],  # Bad timestamp
            ...         e3.to_text()
            ...     ]
            ...     parsed = list(get_entries('\n'.join(log)))

            >>> assert (
            ...     parsed == [e1, e3]
            ... ), f'{all_entries=!r}, {parsed=!r}'
            >>> assert len(warnings) == 1
            >>> assert re.search(
            ...     'trailing text when parsing entry #1',
            ...     str(warnings[0].message),
            ... ), warnings
        """
        header_regex = re.compile(fmt_to_regex(
            HEADER_PATTERN + HEADER_SEP,
            current_pid=r'\d+',
            main_pid=r'\d+|' + re.escape(HEADER_MAIN_INDICATOR),
            obj_id='.+?',
        ))
        for (
            nentries, (ts_match, text_block),
        ) in enumerate(cls._gen_message_blocks_from_text(text)):
            timestamp = parse_timestamp(ts_match.group('timestamp'))
            level = LogLevel(ts_match.group('level'))
            # Strip the block indent
            ts_text = ts_match.group(0)
            assert text_block.startswith(ts_text), (
                # Note: this is purely an internal-consistency thing,
                # and this assertion should never fail
                f'{text_block=!r}, {ts_text=!r}'
            )
            dedented, trailing = cls._dedent_block(text_block, ts_text)
            # Strip the header and parse the relevant info from it
            header_match = header_regex.match(dedented)
            if not header_match:
                # This can happen for whatever reason, e.g. the text of
                # THIS entry is truncated and thus the header can't be
                # matched
                msg = (
                    'failed to parse the following text block after parsing '
                    f'{nentries} entry/-ies: {text_block!r}'
                )
                diagnostics.log.warning('UserWarning: ' + msg)
                warn(msg, stacklevel=3)  # Caller of `.get_entries()`
                continue
            if trailing:
                # The text of THE NEXT entry can be truncated, and thus
                # making its timestamp malformed, causing the two
                # entries to be parsed (squashed) into the same text
                # block by `._gen_message_blocks_from_text()` and
                # resulting in trailing text
                msg = (
                    'got unexpected trailing text when parsing entry '
                    f'#{nentries + 1}: {trailing!r}'
                )
                diagnostics.log.warning('UserWarning: ' + msg)
                warn(msg, stacklevel=3)  # Caller of `.get_entries()`
            current_pid = int(header_match.group('current_pid'))
            main_pid_ = header_match.group('main_pid')
            if main_pid_ == HEADER_MAIN_INDICATOR:
                main_pid = current_pid
            else:
                main_pid = int(main_pid_)
            cache_id = parse_id(header_match.group('obj_id'))
            # The rest of the block is the message proper
            msg = dedented[header_match.end():]
            yield cls(
                timestamp, level, main_pid, current_pid, cache_id, msg,
            )

    @staticmethod
    def _dedent_block(block: str, prefix: str) -> tuple[str, str]:
        r"""
        Example:
            >>> lines = [
            ...     'foo bar: foo',
            ...     '          bar',
            ...     '         baz',
            ... ]
            >>> assert (result := CacheLoggingEntry._dedent_block(
            ...     '\n'.join(lines), 'foo bar: ',
            ... )) == ('foo\n bar\nbaz', ''), result
            >>> lines.append('  some trailing text')
            >>> assert (result := CacheLoggingEntry._dedent_block(
            ...     '\n'.join(lines), 'foo bar: ',
            ... )) == ('foo\n bar\nbaz', '  some trailing text'), result
        """
        assert block.startswith(prefix)
        width = len(prefix)
        block_lines: list[str] = []
        trailing_lines: list[str] = []
        raw_lines = (' ' * width + block[width:]).splitlines()
        for i, line in enumerate(raw_lines):
            prefix = line[:width]
            if prefix and not prefix.isspace():
                trailing_lines.extend(raw_lines[i:])
                break
            block_lines.append(line[width:])
        return '\n'.join(block_lines), '\n'.join(trailing_lines)
