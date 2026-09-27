"""Component contracts.

All loadable agents, tools, skills, adapters, and providers share these metadata contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ComponentKind = Literal["agent", "tool", "skill", "adapter", "llm_provider", "hook"]
SourceType = Literal["builtin", "system", "user", "project", "agent", "plugin", "mcp", "external"]
ComponentScope = Literal["system", "user", "project", "agent", "session"]


@dataclass(frozen=True)
class ComponentSource:
    source_type: SourceType
    scope: ComponentScope
    path: Path | None = None
    plugin_id: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class ComponentSpec:
    kind: ComponentKind
    name: str
    qualified_name: str
    description: str
    source: ComponentSource
    enabled: bool = True
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourcePriorityRule:
    source_type: SourceType
    scope: ComponentScope
    priority: int


@dataclass(frozen=True)
class LoadCacheKey:
    roots: tuple[Path, ...]
    config_hash: str


@dataclass(frozen=True)
class LoadError:
    source: ComponentSource | None
    path: Path | None
    field: str | None
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OverrideRecord:
    name: str
    winner: ComponentSource
    loser: ComponentSource
    reason: str
