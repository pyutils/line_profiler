import pathlib
try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib
from typing import Any, Dict, Mapping, Sequence, Tuple, TypeVar, Union


targets = 'line_profiler_rc.toml', 'pyproject.toml'

K = TypeVar('K')
V = TypeVar('V')
Config = Tuple[Dict[str, Dict[str, Any]], pathlib.Path]
NestedTable = Mapping[K, Union['NestedTable[K, V]', V]]


def find_and_read_config_file(
        *,
        config: Union[str, pathlib.PurePath, None] = None,
        targets: Sequence[Union[str, pathlib.PurePath]] = targets) -> Config:
    ...


def get_subtable(table: NestedTable[K, V], keys: Sequence[K], *,
                 allow_absence: bool = True) -> NestedTable[K, V]:
    ...


def get_config(config: Union[str, pathlib.PurePath, None] = None) -> Config:
    ...


def get_default_config() -> Config:
    ...
