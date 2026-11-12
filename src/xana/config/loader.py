"""Config loader implementations.

Three pieces:

  * ``user_config_dir()`` — resolves ``$XANA_HOME`` or ``~/.xana/config``,
    the machine-level home for system.json (api keys etc. don't follow
    projects). Mirrors yuxu_backup's ``~/.yuxu/config/`` placement.
  * ``JsonSystemConfigReader`` — concrete ``ConfigReader`` that reads
    ``system.json`` from a ``ConfigSource`` and validates it through the
    pydantic ``SystemConfig`` schema; raises on shape errors.
  * ``DefaultConfigProvider`` — concrete ``ConfigProvider`` that loads
    the user system file, overlays the project config (channels / daemon /
    sessions / tools; docs/design/project-design.md §4) and assembles a
    ``RuntimeConfig`` for the harness.
    Missing or unresolved default models fail explicitly. Fake models must
    be selected through a model entry with type="fake".

Pydantic ``SystemConfig`` is also exposed via ``DefaultConfigProvider.
load_system_config()`` for downstream consumers (DSv4 client, etc.) that
need typed access to api keys and model entries.
"""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from xana.config.paths import xana_home
from xana.config.project_config import merge_system_config, project_config_path, read_project_config
from xana.config.contracts import (
    AUTOCOMPACT_BUFFER_TOKENS, FALLBACK_COMPACT_RESERVE, MANUAL_COMPACT_BUFFER_TOKENS,
    SUMMARY_MAX_TOKENS, ConfigSource, ModelConfig, RawConfig, RuntimeConfig,
)
from xana.config.interfaces import ConfigProvider, ConfigReader
from xana.config.system_model import ModelEntry, SystemConfig


# 缺省原始窗口为 1M；已配置/已登记的模型窗口优先，不能用 967k 触发线代替容量。
DEFAULT_CONTEXT_WINDOW = 1_000_000
_SYSTEM_JSON_NAME = "system.json"
_SEED_RESOURCE_PACKAGE = "xana.config.defaults"


def user_config_dir() -> Path:
    return xana_home() / "config"


def default_system_json_source() -> ConfigSource:
    return ConfigSource(scope="system", path=user_config_dir() / _SYSTEM_JSON_NAME, priority=10)


def seed_system_json() -> str:
    return resources.files(_SEED_RESOURCE_PACKAGE).joinpath(_SYSTEM_JSON_NAME).read_text(
        encoding="utf-8"
    )


def ensure_user_system_json(target: Path | None = None) -> Path:
    """Write the seed system.json into the user config dir if missing.

    Returns the live path. chmod 600 because the file holds api keys once
    populated. Safe to call repeatedly — no-op when the file exists.
    """
    live = target if target is not None else user_config_dir() / _SYSTEM_JSON_NAME
    if live.exists():
        return live
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(seed_system_json(), encoding="utf-8")
    try:
        live.chmod(0o600)
    except OSError:
        pass
    return live


class JsonSystemConfigReader(ConfigReader):
    async def read(self, source: ConfigSource) -> RawConfig:
        assert source.path is not None, "JsonSystemConfigReader requires source.path"
        raw = json.loads(source.path.read_text(encoding="utf-8"))
        SystemConfig(**raw)  # validate, raise pydantic ValidationError on shape errors
        return RawConfig(source=source, values=raw)


class DefaultConfigProvider(ConfigProvider):
    """Loads ~/.xana/config/system.json (or $XANA_HOME) and produces a
    RuntimeConfig pinned to a project root.

    The pydantic SystemConfig is cached on the instance after ``load()`` so
    downstream callers can grab it via ``load_system_config()`` without
    re-reading.
    """

    def __init__(self, reader: ConfigReader | None = None, source: ConfigSource | None = None,
                 project_config: Path | None = None) -> None:
        """project_config:本项目的配置文件(缺省 projects/<当前项目>/config.json),覆盖用户配置的项目键。"""
        self._reader = reader or JsonSystemConfigReader()
        self._source = source or default_system_json_source()
        self._project_config = project_config
        self._system: SystemConfig | None = None
        self._raw: RawConfig | None = None

    @property
    def project_config_path(self) -> Path:
        return self._project_config if self._project_config is not None else project_config_path()

    async def load(self, project_root: Path) -> RuntimeConfig:
        assert self._source.path is not None
        ensure_user_system_json(self._source.path)
        raw = await self._reader.read(self._source)
        self._raw = raw
        self._system = merge_system_config(raw.values, read_project_config(self.project_config_path))
        default_model = _resolve_default_model(self._system)
        return RuntimeConfig(
            project_root=project_root,
            data_root=project_root / ".xana" / "data",
            default_model=default_model,
            raw=(raw,),
        )

    def load_system_config(self) -> SystemConfig:
        assert self._system is not None, "call load() first"
        return self._system


def resolve_model_config(entry: ModelEntry) -> ModelConfig:
    """模型预算唯一推导点，输出限制与摘要请求共用同一结果。

    完整配置 C/O：R=min(O,20k)，E=C-R，自动线 E-13k，阻断线 E-3k。
    C/O 任一未配置：自动线直接 C-64Ki，不再多扣13k；有效窗口为自动线+13k，
    阻断线仍为有效窗口-3k。fallback 总预留包含13k，而不是额外叠加。
    显式 compact_override 只覆盖自动线，不改模型容量、输出或阻断线。
    """
    window = entry.context_window if entry.context_window is not None else DEFAULT_CONTEXT_WINDOW
    if entry.max_tokens is not None and entry.max_tokens >= window:
        raise ValueError(f"模型 {entry.name!r} 的 max_tokens 必须小于 context_window")
    summary_max = min(entry.max_tokens, SUMMARY_MAX_TOKENS) if entry.max_tokens is not None else SUMMARY_MAX_TOKENS
    if entry.context_window is None or entry.max_tokens is None:
        trigger = window - FALLBACK_COMPACT_RESERVE
        effective = trigger + AUTOCOMPACT_BUFFER_TOKENS
    else:
        effective = window - summary_max
        trigger = effective - AUTOCOMPACT_BUFFER_TOKENS
    return ModelConfig(
        provider=entry.type, model=entry.name, max_tokens=entry.max_tokens,
        context_window=window,
        force_compact=entry.compact_override if entry.compact_override is not None else max(0, trigger),
        blocking_limit=max(0, effective - MANUAL_COMPACT_BUFFER_TOKENS),
        summary_max_tokens=summary_max, cache_ttl_seconds=entry.cache_ttl_seconds,
        input_modalities=tuple(entry.input_modalities),
    )


def _resolve_default_model(system: SystemConfig) -> ModelConfig:
    chat_name = system.default_model.chat
    if not chat_name or not chat_name.strip():
        raise ValueError("未配置 default_model.chat；请运行 xana setup 或在 system.json 中配置默认模型")
    entry = system.model(chat_name)
    if entry is None:
        raise ValueError(f"默认模型 {chat_name!r} 不在 system.json models[] 中")
    return resolve_model_config(entry)
