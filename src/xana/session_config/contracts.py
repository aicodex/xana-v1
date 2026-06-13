"""SessionConfig: per-session runtime knobs the user toggles via /model, /effort, etc.

This is intentionally small and frozen. The CLI persists it via SessionConfigStore
and re-reads before each turn — there is no mid-turn mutation. Fields that map
1:1 onto HarnessContext (model, effort) are threaded through ChatAgent; the rest
live in HarnessContext.config_snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SessionConfig:
    session_id: str
    model: str | None = None
    effort: str | int | None = None
    streaming: bool = True
    thinking: bool = True  # DS API 默认 thinking enabled,与之对齐(2026-06-13 授权)
    search_provider: str | None = None
    project_root: str | None = None
    async_mode: bool = True  # P3 完成,默认 async
    metadata: dict[str, Any] = field(default_factory=dict)
