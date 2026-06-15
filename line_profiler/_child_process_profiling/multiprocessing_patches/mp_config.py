from __future__ import annotations

import dataclasses
from typing import Any, NamedTuple, final
from typing_extensions import Self

from ...toml_config import ConfigSource
from ..cache import LineProfilingCache


__all__ = ('MPConfig',)


@final
class _PollerArgs(NamedTuple):
    cooldown: float
    timeout: float
    on_timeout: str | None

    @classmethod
    def new(cls, cooldown: Any, timeout: Any, on_timeout: Any) -> Self:
        try:
            cd = max(float(cooldown), 0)
        except (TypeError, ValueError):
            cd = 0
        try:
            to = max(float(timeout), 0)
        except (TypeError, ValueError):
            to = 0
        try:
            ot: str | None = on_timeout.lower()
        except Exception:  # Fallback (use `_Poller`'s default)
            ot = None
        return cls(cd, to, ot)


@final
@dataclasses.dataclass
class MPConfig:
    """
    Consolidate the config options into a structured object.
    """
    catch_sigterm: bool
    patches: dict[str, bool]
    polling: _PollerArgs

    @classmethod
    def from_config(cls, config: ConfigSource) -> Self:
        loaded = (
            config
            .get_subconfig('child_processes', 'multiprocessing')
            .conf_dict
        )
        polling = _PollerArgs.new(**loaded['polling'])
        return cls(
            catch_sigterm=loaded['catch_sigterm'],
            patches=dict(loaded['patches']),
            polling=polling,
        )

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
