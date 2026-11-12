"""Goal tool identifiers/schemas and Chinese descriptions translated from Codex 8703d32b."""
from __future__ import annotations

import json

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.harness_sdk.goal import GoalService
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

_DEFINITIONS = {
    "create_goal": ("仅在用户或系统/开发者指令明确要求时创建目标，不要从普通任务推断目标。"
        "仅在明确要求 token 预算时设置 token_budget。已有未完成目标时创建失败；update_goal 仅用于更新状态。",
        {"objective": {"type": "string", "description": "要推进的具体目标。当前没有目标或目标已完成时，开始一个新的活动目标。"},
         "token_budget": {"type": "integer", "description": "新目标的正整数 token 预算。未明确要求时省略。"}}, ["objective"]),
    "get_goal": ("获取当前会话的目标，包括状态、预算、token 和运行时间用量，以及剩余 token 预算。", {}, []),
    "update_goal": ("更新当前目标。仅在用户明确要求时暂停；仅在目标实际达成且没有剩余工作时标记 complete。"
        "只有同一阻塞连续出现至少三个目标回合，且无法继续推进时才标记 blocked；恢复后重新计算阻塞观察。"
        "不要因工作困难、耗时、尚未完成或预算将尽就标记完成。此工具不用于恢复目标或修改预算。",
        {"status": {"type": "string", "enum": ["paused", "blocked", "complete"]}}, ["status"]),
}


class GoalTool(Tool):
    def __init__(self, service: GoalService, name: str) -> None:
        self._service = service
        description, properties, required = _DEFINITIONS[name]
        self._spec = ToolSpec(component=ComponentSpec(kind="tool", name=name, qualified_name=f"builtin.{name}",
            description=description, source=ComponentSource(source_type="builtin", scope="system")),
            input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            temporal="static", toolset="session", read_only=True,
            display=ToolDisplay(category="plan", verb="目标", summary_args=("status", "objective")))

    def spec(self) -> ToolSpec:
        return self._spec

    async def validate(self, invocation: ToolInvocation) -> None:
        if self._spec.component.name == "create_goal" and not str(invocation.input.get("objective", "")).strip():
            raise ValueError("goal objective required")
        if self._spec.component.name == "update_goal" and invocation.input.get("status") not in {"paused", "blocked", "complete"}:
            raise ValueError("invalid goal status")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult("allow")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        name = self._spec.component.name
        if name == "create_goal":
            self._service.create(context.session_id, invocation.input["objective"], token_budget=invocation.input.get("token_budget"))
        elif name == "update_goal":
            self._service.update_from_tool(context.session_id, invocation.input["status"], context.goal_id)
        content = {"goal": self._service.view(context.session_id)}
        return ToolResult(ok=True, content=content, rendered=json.dumps(content, ensure_ascii=False))
