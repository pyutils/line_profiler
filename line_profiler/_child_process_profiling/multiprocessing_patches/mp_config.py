from __future__ import annotations

import dataclasses
from typing import final
from typing_extensions import Self

from ...toml_config import ConfigSource
from ..cache import LineProfilingCache


__all__ = ('MPConfig',)


@final
@dataclasses.dataclass
class MPConfig:
    """
    Consolidate the config options into a structured object.

    Notes:

        - This corresponds to the
          ``tool.child_processes.multiprocessing`` table in
          ``line_profiler.toml``.

        - While the object has currently only a single field, it is
          intentionally kept this way to make the config scheme
          extensible.
    """
    patches: dict[str, bool]

    @classmethod
    def from_config(cls, config: ConfigSource) -> Self:
        loaded = (
            config
            .get_subconfig('child_processes', 'multiprocessing')
            .conf_dict
        )
        return cls(patches=dict(loaded['patches']))

    @classmethod
    def from_cache(cls, cache: LineProfilingCache) -> Self:
        key = 'mp_config'
        try:
            return cache._additional_data[key]
        except KeyError:
            config = cls.from_config(cache._config_source)
            return cache._additional_data.setdefault(key, config)

    @classmethod
    def get_defaults(cls) -> Self:
        namespace = globals()
        name = '_DEFAULT_CONFIG'
        try:
            return namespace[name]
        except KeyError:
            defaults = cls.from_config(ConfigSource.from_default(copy=False))
            return namespace.setdefault(name, defaults)
