from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Mapping, Sequence, Any, Self, TypeVar


TARGETS = 'line_profiler.toml', 'pyproject.toml'
ENV_VAR = 'LINE_PROFILER_RC'

K = TypeVar('K')
V = TypeVar('V')
Config = tuple[dict[str, dict[str, Any]], Path]
NestedTable = Mapping[K, 'NestedTable[K, V]' | V]


@dataclass
class ConfigSource:
    conf_dict: dict[str, Any]
    path: Path
    subtable: list[str]

    def copy(self) -> Self:
        ...

    def get_subconfig(self, *headers: str,
                      allow_absence: bool = False, copy: bool = False) -> Self:
        ...

    @classmethod
    def from_default(cls, *, copy: bool = True) -> Self:
        ...

    @classmethod
    def from_config(cls, config: str | PathLike | bool | None = None, *,
                    read_env: bool = True) -> Self:
        ...


def find_and_read_config_file(
        *,
        config: str | PathLike | None = None,
        env_var: str | None = ENV_VAR,
        targets: Sequence[str | PathLike] = TARGETS) -> Config:
    ...


def get_subtable(table: NestedTable[K, V], keys: Sequence[K], *,
                 allow_absence: bool = True) -> NestedTable[K, V]:
    ...


def get_headers(table: NestedTable[K, Any], *,
                include_implied: bool = False) -> set[tuple[K, ...]]:
    ...
