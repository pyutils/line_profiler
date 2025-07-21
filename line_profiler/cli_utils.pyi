"""
Shared utilities between the :command:`python -m line_profiler` and
:command:`kernprof` CLI tools.
"""
import argparse
import pathlib
from typing import Protocol, Sequence, Tuple, TypeVar, Union

from line_profiler.toml_config import ConfigSource


A_co = TypeVar('A_co', bound='ActionLike', covariant=True)


class ActionLike(Protocol):
    def __call__(self, parser: 'ParserLike',
                 namespace: argparse.Namespace,
                 values: Sequence,
                 option_string: Union[str, None] = None) -> None:
        ...

    def format_usage(self) -> str:
        ...


class ParserLike(Protocol[A_co]):
    def add_argument(self, arg: str, /, *args: str, **kwargs) -> A_co:
        ...

    @property
    def prefix_chars(self) -> str:
        ...


def add_argument(parser_like: ParserLike[A_co], arg: str, /, *args: str,
                 hide_complementary_options: bool = True, **kwargs) -> A_co:
    ...


def get_cli_config(subtable: str, /, *args, **kwargs) -> ConfigSource:
    ...


def get_python_executable() -> str:
    ...


def positive_float(value: str) -> float:
    ...


def boolean(value: str, *,
            fallback: Union[bool, None] = None, invert: bool = False) -> bool:
    ...


def short_string_path(path: Union[str, pathlib.PurePath]) -> str:
    ...
