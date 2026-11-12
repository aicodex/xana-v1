"""SessionConfig: per-session runtime knobs the user toggles via /model, /effort, etc.

This is intentionally small and frozen. The CLI persists it via SessionConfigStore
and re-reads before each turn — there is no mid-turn mutation. Fields that map
1:1 onto HarnessContext (model, effort) are threaded through ChatAgent; the rest
live in HarnessContext.config_snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from xana.policy.contracts import PermissionMode


@dataclass(frozen=True)
class SessionConfig:
    session_id: str
    model: str | None = None
    effort: str | int | None = None
    streaming: bool = True
    thinking: bool = True  # DS API 默认 thinking enabled,与之对齐(2026-06-13 授权)
    search_provider: str | None = None
    # 工具过程显示模式(detailed / grouped / summary);None = 渠道默认(飞书、TG grouped,CLI detailed)
    view_mode: str | None = None
    # 会话绑定的 agent(/agent 设置,docs/design/agent-delegation-design.md §3.1);None = 默认 agent
    agent: str | None = None
    # 会话创建时由 AgentSpec.prompt_profile 解析后写入(PromptProfile.to_dict),之后只读
    prompt_profile: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    permission_mode: PermissionMode | None = None  # None inherits parent, or yolo for a root session.
    permission_grants: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.permission_mode not in {None, "manual", "yolo", "auto"}:
            raise ValueError("invalid permission mode")
