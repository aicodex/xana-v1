"""report_goal:目标模式下由模型主动声明目标终态(done / blocked / failed)。

只在 PromptProfile.goal_mode 的会话中出现(harness 按 profile 过滤 schema)。
blocked / failed 直接结束本轮,不再交给评估器;done 仍由评估器核实。
"""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.harness_sdk.goal import GOAL_STATUSES, REPORT_GOAL_TOOL
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

_SPEC = ToolSpec(
    toolset="session",
    component=ComponentSpec(
        kind="tool",
        name=REPORT_GOAL_TOOL,
        qualified_name=f"builtin.{REPORT_GOAL_TOOL}",
        description=("声明当前目标的终态。done:目标已达成(harness 会核实);"
                     "blocked:被外部原因阻断,需要人处理;failed:确认无法达成。附一句原因。"),
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(GOAL_STATUSES)},
            "reason": {"type": "string", "description": "一句话说明原因或证据"},
        },
        "required": ["status", "reason"],
    },
    read_only=True,
    display=ToolDisplay(category="plan", verb="报告目标状态", summary_args=("status", "reason")),
)


class ReportGoalTool(Tool):
    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if invocation.input.get("status") not in GOAL_STATUSES:
            raise ValueError(f"report_goal: status 必须是 {GOAL_STATUSES} 之一")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        status = str(invocation.input["status"])
        reason = str(invocation.input.get("reason", ""))
        return ToolResult(ok=True, content={"status": status, "reason": reason},
                          rendered=f"已记录目标状态:{status}({reason})",
                          metadata={"goal_status": status, "goal_reason": reason})
