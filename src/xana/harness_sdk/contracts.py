"""Harness SDK contracts.

The harness owns turn lifecycle and event stream shape. Run state is owned by state.RunRecord.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TYPE_CHECKING

from xana.policy.contracts import PermissionContext
from xana.state.contracts import UsageSnapshot

if TYPE_CHECKING:
    from xana.hooks.interfaces import HookRunner
    from xana.llm_driver.interfaces import ModelRegistry
    from xana.memory.interfaces import (
        ActiveContextStore,
        HistoryMirror,
        MemoryInjectorRegistry,
        MemorySearchEngine,
        MemoryTree,
    )
    from xana.observability.interfaces import TraceSink
    from xana.skills.interfaces import SkillRegistry
    from xana.state.interfaces import StateRepository
    from xana.tools.interfaces import ToolRegistry

HarnessEventKind = Literal["turn_started", "model_delta", "tool_started", "tool_finished", "paused", "cancelled", "continuation", "final", "error"]


@dataclass
class AbortSignal:
    """Mutable per-run abort flag — gateway flips `aborted=True` via
    `trigger()` on stop click, and the harness's `_run_tool_with_abort`
    polls `.aborted` to interrupt a currently-running tool. Mutability is
    required so a flip on an in-flight turn is visible to the loop that
    captured this instance in its `HarnessContext`."""
    aborted: bool = False
    reason: str | None = None

    def trigger(self, reason: str) -> None:
        self.aborted = True
        self.reason = reason


@dataclass(frozen=True)
class ProgressEvent:
    session_id: str
    run_id: str | None
    message: str
    payload: dict[str, Any] = field(default_factory=dict)


PromptMode = Literal["interactive", "unattended"]
TimeSignal = Literal["off", "elapsed", "budget"]

_PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    # 交互:照 Claude Code 默认不额外干预(它的"开工前说一句""如实报告"已在基础 system 中)
    "interactive": dict(standing_order=False, answered_as_done=False, explore_first=False,
                        grounded_progress=False, time_signal="off", goal_mode=False),
    # 无人值守(长程):照 CC 只加自主运行段(随 mode)+ 目标模式评估器;其余都按 agent 自愿开启,
    # 给模型留探索空间,把"做完没有"交给验证机制(verify 检查 + 评估器)
    "unattended": dict(standing_order=False, answered_as_done=False, explore_first=False,
                       grounded_progress=False, time_signal="off", goal_mode=True),
}


@dataclass(frozen=True)
class PromptProfile:
    """会话级提示词开关(turn-control-design §4)。会话创建时固定,之后只读。

    开关文本在 system 末尾按固定顺序拼接,见 ``system_prompt.profile_sections``。
    """
    mode: PromptMode = "interactive"
    standing_order: bool = False
    answered_as_done: bool = False
    explore_first: bool = False
    grounded_progress: bool = False
    time_signal: TimeSignal = "off"
    goal_mode: bool = False

    @classmethod
    def for_mode(cls, mode: PromptMode = "interactive", **overrides: Any) -> "PromptProfile":
        if mode not in _PROFILE_DEFAULTS:
            raise ValueError(f"未知 prompt_profile.mode {mode!r}")
        unknown = set(overrides) - set(_PROFILE_DEFAULTS[mode])
        if unknown:
            raise ValueError(f"未知 prompt_profile 字段 {sorted(unknown)}")
        return cls(mode=mode, **{**_PROFILE_DEFAULTS[mode], **overrides})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PromptProfile":
        data = dict(data)
        return cls.for_mode(data.pop("mode", "interactive"), **data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerifyCheck:
    """回合结束时执行的检查命令(AGENT.md ``verify:``)。退出码 0 = 通过。"""
    cmd: str
    timeout_s: float = 300.0

    @classmethod
    def from_raw(cls, raw: Any) -> "VerifyCheck":
        if isinstance(raw, str) and raw.strip():
            return cls(cmd=raw.strip())
        if isinstance(raw, dict) and isinstance(raw.get("cmd"), str) and raw["cmd"].strip():
            return cls(cmd=raw["cmd"].strip(), timeout_s=float(raw.get("timeout_s", 300.0)))
        raise ValueError(f"verify 项必须是命令字符串或 {{cmd, timeout_s}}: {raw!r}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HarnessRequest:
    session_id: str
    agent_name: str
    user_input: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessContext:
    session_id: str
    agent_name: str
    cwd: str
    model: str
    effort: str | int | None = None
    run_id: str | None = None
    permission: PermissionContext | None = None
    state_repository: "StateRepository | None" = None
    active_context_store: "ActiveContextStore | None" = None
    history_mirror: "HistoryMirror | None" = None
    memory_tree: "MemoryTree | None" = None
    memory_search_engine: "MemorySearchEngine | None" = None
    memory_injector_registry: "MemoryInjectorRegistry | None" = None
    tool_registry: "ToolRegistry | None" = None
    skill_registry: "SkillRegistry | None" = None
    model_registry: "ModelRegistry | None" = None
    hook_runner: "HookRunner | None" = None
    trace_sink: "TraceSink | None" = None
    progress_sink: "ProgressSink | None" = None
    abort: AbortSignal = field(default_factory=AbortSignal)
    async_mode: bool = False
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessEvent:
    kind: HarnessEventKind
    session_id: str
    run_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    usage: UsageSnapshot | None = None
