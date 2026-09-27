"""Config loader implementations.

Three pieces:

  * ``user_config_dir()`` — resolves ``$XANA_HOME`` or ``~/.xana/config``,
    the machine-level home for system.json (api keys etc. don't follow
    projects). Mirrors yuxu_backup's ``~/.yuxu/config/`` placement.
  * ``JsonSystemConfigReader`` — concrete ``ConfigReader`` that reads
    ``system.json`` from a ``ConfigSource`` and validates it through the
    pydantic ``SystemConfig`` schema; raises on shape errors.
  * ``DefaultConfigProvider`` — concrete ``ConfigProvider`` that loads
    the system file and assembles a ``RuntimeConfig`` for the harness.
    When no default model is configured (fresh setup, empty models[]),
    it falls back to ``provider="fake"`` so boot can still run.

Pydantic ``SystemConfig`` is also exposed via ``DefaultConfigProvider.
load_system_config()`` for downstream consumers (DSv4 client, etc.) that
need typed access to api keys and model entries.
"""
from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path

from xana.config.paths import xana_home
from xana.config.contracts import ConfigSource, ModelConfig, RawConfig, RuntimeConfig
from xana.config.interfaces import ConfigProvider, ConfigReader
from xana.config.system_model import ModelEntry, SystemConfig


_FALLBACK_PROVIDER = "fake"
_DEFAULT_CONTEXT_WINDOW = 967_000  # 967k 预算 — 用户指定;entry.context_window 可覆盖
_DEFAULT_MAX_OUTPUT = 16_384  # 与 anthropic_deepseek._DEFAULT_MAX_TOKENS 对齐,否则输出可能在 compact 触发前撑爆窗口
_COMPACT_HEADROOM = 13_000  # CC 风格: force_compact = ctx - max_output - 13K
_FALLBACK_MODEL = "fake-model"
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


def write_system_config(path: Path, system: SystemConfig) -> None:
    """Atomically persist ``system`` to ``path`` (chmod 600).

    Used as a mirror write by SessionResolver — in-memory state is
    canonical (I15), this is the durable side-mirror. Atomic via
    tmp + fsync + os.replace so partial writes never corrupt the file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = system.model_dump(mode="json", exclude_none=False)
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    # Idempotent: skip the write if disk already matches. Avoids spurious
    # inode churn when resolve() hits an existing entry on a hot path.
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == data:
                return
        except OSError:
            pass
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


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

    def __init__(self, reader: ConfigReader | None = None, source: ConfigSource | None = None) -> None:
        self._reader = reader or JsonSystemConfigReader()
        self._source = source or default_system_json_source()
        self._system: SystemConfig | None = None
        self._raw: RawConfig | None = None

    async def load(self, project_root: Path) -> RuntimeConfig:
        assert self._source.path is not None
        ensure_user_system_json(self._source.path)
        raw = await self._reader.read(self._source)
        self._raw = raw
        self._system = SystemConfig(**raw.values)
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


def derive_compact_thresholds(entry: "ModelEntry") -> tuple[int, int]:
    """(context_window, force_compact) — compact_override 优先,0 也有效。"""
    ctx_win = entry.context_window if entry.context_window is not None else _DEFAULT_CONTEXT_WINDOW
    force_at = (entry.compact_override if entry.compact_override is not None
                else ctx_win - _DEFAULT_MAX_OUTPUT - _COMPACT_HEADROOM)
    return ctx_win, force_at


def _resolve_default_model(system: SystemConfig) -> ModelConfig:
    chat_name = system.default_model.chat
    if chat_name is not None:
        entry = system.model(chat_name)
        if entry is not None:
            ctx_win, force_at = derive_compact_thresholds(entry)
            return ModelConfig(
                provider=entry.type, model=entry.name,
                context_window=ctx_win, force_compact=force_at,
            )
    return ModelConfig(provider=_FALLBACK_PROVIDER, model=_FALLBACK_MODEL)
