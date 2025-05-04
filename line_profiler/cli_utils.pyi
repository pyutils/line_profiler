"""
Shared utilities between the `python -m line_profiler` and `kernprof`
CLI tools.
"""
import argparse
import pathlib
from .toml_config import get_config
from typing import Protocol, Sequence, Tuple, TypeVar, Union


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
    def add_argument(self, *args, **kwargs) -> A_co:
        ...


def add_argument(parser_like: ParserLike[A_co], *args,
                 hide_complementary_options: bool = True, **kwargs) -> A_co:
    ...


def get_cli_config(subtable: str, /,
                   *args, **kwargs) -> Tuple[dict, pathlib.Path]:
    ...


def get_python_executable() -> str:
    ...


def positive_float(value: str) -> float:
    ...


def short_string_path(path: Union[str, pathlib.PurePath]) -> str:
    ...
