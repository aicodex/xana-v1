"""Config contracts.

Read this when loading or merging system/project/agent settings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ConfigScope = Literal["system", "project", "agent", "session"]


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


@dataclass(frozen=True)
class RuntimeConfig:
    project_root: Path
    data_root: Path
    default_model: ModelConfig
    raw: tuple[RawConfig, ...] = ()
