"""Config contracts.

Read this when loading or merging system/project/agent settings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from xana.multimodal.contracts import InputModality

ConfigScope = Literal["system", "project", "agent", "session"]

SUMMARY_MAX_TOKENS = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000
FALLBACK_COMPACT_RESERVE = 64 * 1024
DEFAULT_CACHE_TTL_SECONDS = 3600


@dataclass(frozen=True)
class ConfigSource:
    scope: ConfigScope
    path: Path | None
    priority: int


@dataclass(frozen=True)
class RawConfig:
    source: ConfigSource
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    effort: str | int | None = None
    max_tokens: int | None = None
    context_window: int | None = None  # 扣除压缩/输出预留之前的配置窗口。
    force_compact: int | None = None  # 从窗口推导的自动压缩触发线，不能当作模型容量。
    blocking_limit: int | None = None
    summary_max_tokens: int = SUMMARY_MAX_TOKENS
    input_modalities: tuple[InputModality, ...] = ()
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS


@dataclass(frozen=True)
class RuntimeConfig:
    project_root: Path
    data_root: Path
    default_model: ModelConfig
    raw: tuple[RawConfig, ...] = ()
