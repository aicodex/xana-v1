"""Tool contracts.

Tools declare validation, permission, sandbox, execution input, and output shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from xana.component.contracts import ComponentSpec
from xana.policy.contracts import PermissionContext, SandboxPolicySpec

PermissionDecision = Literal["allow", "ask", "deny"]
ToolConcurrency = Literal["parallel", "serialized", "exclusive"]
InterruptBehavior = Literal["cancel", "block", "ignore"]

# Structured failure attribution.
#
# Superset of codex ToolItemFailureKind (`tool_error`, `approval_denied`,
# `approval_aborted`, `sandbox_denied`, `policy_forbidden`) plus the additional
# kinds CC and yuxu need to distinguish at the observability layer.
#
# Semantics — only set when `ToolResult.ok is False`:
#   validation         input failed Tool.validate or schema check
#   policy_forbidden   PermissionPolicy.decide returned "deny" (config rule)
#   approval_denied    user said deny via ApprovalProvider
#   approval_aborted   approval timed out / user aborted before deciding
#   sandbox_denied     sandbox layer refused (e.g. write outside writable_roots)
#   timeout            execution exceeded ToolSpec.timeout_s
#   execution          runtime error inside the tool body (codex tool_error)
#   cancelled          caller-initiated cancel via Tool.cancel
#   not_found          target resource (file / endpoint / record) absent
#   unsupported        capability missing (MCP server down, feature gated)
ToolFailureKind = Literal[
    "validation",
    "policy_forbidden",
    "approval_denied",
    "approval_aborted",
    "sandbox_denied",
    "timeout",
    "execution",
    "cancelled",
    "not_found",
    "unsupported",
]


ToolCategory = Literal["read", "search_code", "edit", "execute", "web_search", "web_open",
                       "research", "memory", "plan", "delegate", "ask", "other"]


@dataclass(frozen=True)
class ToolDisplay:
    """工具的默认显示配置(docs/design/tool-display-modes.md §2)。渠道按显示模式据此分组、计数、生成标题;
    进行中的文案由 category 决定(渲染层文案表),工具只给完成态动词与计数单位。"""
    category: ToolCategory
    verb: str                              # 完成态动词,如 "搜索网页"、"打开"、"运行"
    unit: str = "次"                        # 计数单位,如 "次"、"个网页"、"条命令"
    count_arg: str | None = None           # 按某个数组参数的长度计数;None = 每次调用计 1
    summary_args: tuple[str, ...] = ()     # 单行摘要取哪些参数(按优先级)
    result: Literal["none", "preview", "full"] = "preview"

    def count(self, args: dict[str, Any]) -> int:
        if self.count_arg:
            value = args.get(self.count_arg)
            if isinstance(value, (list, tuple)):
                return max(1, len(value))
        return 1

    def to_payload(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"category": self.category, "verb": self.verb, "unit": self.unit,
                "count": self.count(args), "summary_args": list(self.summary_args), "result": self.result}


@dataclass(frozen=True)
class ToolSpec:
    component: ComponentSpec
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    # `read_only` 也用于 cancel/取消 语义(P5.1):
    #   True  → 取消时立即 abort tool,resume = 重跑该工具
    #   False → 取消时等 2 秒宽限,完成则正常写,超时强杀 + 注入
    #           synthetic warning(P5.2),resume = fork 新 run
    read_only: bool = False
    destructive: bool = False
    open_world: bool = False
    concurrency: ToolConcurrency = "serialized"
    interrupt_behavior: InterruptBehavior = "block"
    permission_policy: str | None = None
    sandbox_policy: SandboxPolicySpec | None = None
    timeout_s: float | None = None
    max_result_chars: int = 50_000
    user_facing_name: str | None = None
    defer: bool = False  # True = 隐藏工具:模型只见名字,经 tool_search 揭示后才发完整 schema
    display: ToolDisplay | None = None  # 默认显示配置;None = 渲染层兜底(类别 other,动词用工具名)
    # True = provider 有等价的原生服务端能力(如 deepseek 原生联网搜索)时让位:harness 从 schema 剔除本工具,
    # 改由 provider 注入原生能力。仅当本工具在该 agent 的可用范围内时才触发。
    yields_to_native: bool = False
    # 所属工具组(docs/design/agent-composition-design.md §3.1);"" = 未分组,只能按工具名或 "*" 选中
    toolset: str = ""


@dataclass(frozen=True)
class Toolset:
    """工具组:agent 按组选工具。default=True 的组构成"默认组合"(agent 不写 tools 时拿到的工具)。"""
    name: str
    description: str
    default: bool = False


@dataclass(frozen=True)
class ToolInvocation:
    tool_name: str
    input: dict[str, Any]
    call_id: str
    session_id: str
    run_id: str | None = None


@dataclass(frozen=True)
class ToolContext:
    session_id: str
    agent_name: str
    cwd: str
    permission: PermissionContext
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # 执行中上报进度(一句给人看的话,如"正在阅读 18/40 个网页");harness 转成 tool_progress 事件
    # 由渠道显示,不进入模型上下文。None = 调用方不接收进度。
    report_progress: Callable[[str], None] | None = None
    # 结构化活动(比 report_progress 细:分道、类型),渠道显示为可展开的时间线;None = 调用方不接收
    report_activity: Callable[[ToolActivity], None] | None = None
    # 本 run 可用的工具名(agent 工具选择解析结果);None = 不限。委派 / 创建 agent 时据此收窄权限
    available_tools: frozenset[str] | None = None
    # 本 run 可见的技能名;None = 全部(skill_load 据此拒绝不可见技能)
    visible_skills: frozenset[str] | None = None


ActivityKind = Literal["thinking", "search", "open", "tool", "done", "error", "note"]


@dataclass(frozen=True)
class ToolActivity:
    """工具执行中的一条结构化活动(docs/design/lead-research-design.md「进度展示」):
    同一调用的活动按 lane(分道,如 plan / r1 / synth)累积成时间线显示;text 同时作为最新一行状态。"""
    lane: str
    text: str
    kind: ActivityKind = "note"
    lane_title: str = ""        # 分道标题(首次出现时给出即可)


@dataclass(frozen=True)
class ToolArtifact:
    name: str
    path: Path | None = None
    content_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDiff:
    path: Path
    diff: str


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: Any
    structured_content: dict[str, Any] | None = None
    error: str | None = None
    failure_kind: ToolFailureKind | None = None
    rendered: str | None = None
    artifacts: tuple[ToolArtifact, ...] = ()
    diffs: tuple[ToolDiff, ...] = ()
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    truncated: bool = False
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
