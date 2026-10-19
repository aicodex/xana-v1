"""Agent contracts.

Agents configure harness usage and expose chat/control APIs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from xana.component.contracts import ComponentSpec
from xana.harness_sdk.contracts import HarnessEvent, PromptProfile, VerifyCheck

AgentEventKind = Literal["draft", "tool", "final", "control_result", "error", "cancelled"]


@dataclass(frozen=True)
class AgentSpec:
    component: ComponentSpec
    prompt_id: str
    when_to_use: str = ""
    # 工具选择表达式(docs/design/agent-composition-design.md §3.3):default / 组名 / 工具名 / "*";
    # () = 零工具。disallowed_tools 在其后再减,同样接受组名。
    tools: tuple[str, ...] = ("default",)
    disallowed_tools: tuple[str, ...] = ()
    # 技能选择(docs/design/project-design.md §8):None = 全部可见;() = 没有技能;名单 = 只这些;含 "*" = 全部
    skills: tuple[str, ...] | None = None
    model: str | None = None
    effort: str | int | None = None
    max_turns: int | None = None
    # None → xana 默认系统提示词;字符串 → 系统提示词模板(§3.4):${base_prompt} 嵌入默认提示词,
    # ${env} 环境段,${extensions} 扩展片段;不含占位符 = 完全替换
    system_prompt: str | None = None
    # 会话级提示词开关;None = 交互模式默认值。会话创建时固化进 SessionConfig
    prompt_profile: PromptProfile | None = None
    # 回合结束时的检查命令(命令型验证,对应 CC 命令型 Stop hook)
    verify: tuple[VerifyCheck, ...] = ()


@dataclass(frozen=True)
class AgentRequest:
    session_id: str
    text: str
    source: str
    target_agent: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    # 本 run 可用工具的上限(委派时 = 调用方可用工具 − 委派 / 会话控制类工具);None = 不设上限。
    # harness 在 agent 自身工具选择的解析结果上再取交集(docs/design/agent-delegation-design.md §3.2)
    tool_ceiling: frozenset[str] | None = None


@dataclass(frozen=True)
class AgentControlRequest:
    session_id: str
    op: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEvent:
    kind: AgentEventKind
    session_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    harness_event: HarnessEvent | None = None
