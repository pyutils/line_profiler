from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import (List, Dict, Set, Tuple,
                    Mapping, Sequence,
                    Any, Self, TypeVar, Union)


TARGETS = 'line_profiler.toml', 'pyproject.toml'
ENV_VAR = 'LINE_PROFILER_RC'

K = TypeVar('K')
V = TypeVar('V')
Config = Tuple[Dict[str, Dict[str, Any]], Path]
NestedTable = Mapping[K, Union['NestedTable[K, V]', V]]


@dataclass
class ConfigSource:
    conf_dict: Dict[str, Any]
    source: Path
    subtable: List[str]

    def copy(self) -> Self:
        ...

    def get_subconfig(self, *headers: str,
                      allow_absence: bool = False, copy: bool = False) -> Self:
        ...

    @classmethod
    def from_default(cls, *, copy: bool = True) -> Self:
        ...

    @classmethod
    def from_config(cls, config: Union[str, PurePath, bool, None] = None, *,
                    read_env: bool = True) -> Self:
        ...


def find_and_read_config_file(
        *,
        config: Union[str, PurePath, None] = None,
        env_var: Union[str, None] = ENV_VAR,
        targets: Sequence[Union[str, PurePath]] = TARGETS) -> Config:
    ...


def get_subtable(table: NestedTable[K, V], keys: Sequence[K], *,
                 allow_absence: bool = True) -> NestedTable[K, V]:
    ...


def get_headers(table: NestedTable[K, Any], *,
                include_implied: bool = False) -> Set[Tuple[K, ...]]:
    ...
