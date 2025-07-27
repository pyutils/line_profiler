"""
Shared utilities between the :command:`python -m line_profiler` and
:command:`kernprof` CLI tools.
"""
import argparse
import pathlib
from os import PathLike
from typing import Protocol, Sequence, Tuple, TypeVar

from line_profiler.toml_config import ConfigSource


P_con = TypeVar('P_con', bound='ParserLike', contravariant=True)
A_co = TypeVar('A_co', bound='ActionLike', covariant=True)


class ActionLike(Protocol[P_con]):
    def __call__(self, parser: P_con,
                 namespace: argparse.Namespace,
                 values: str | Sequence | None,
                 option_string: str | None = None) -> None:
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
            fallback: bool | None = None, invert: bool = False) -> bool:
    ...


def short_string_path(path: str | PathLike[str]) -> str:
    ...
