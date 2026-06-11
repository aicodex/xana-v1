"""EnterPlanMode: flip the session into 'plan' mode.

Plan mode (cc): the agent should focus on exploration and design, NOT
execute mutating tools. We persist the marker on SessionConfig.metadata
so the next turn's HarnessContext can refuse destructive Tool.execute()
calls. Tools opt in by checking PermissionContext.mode.

If no SessionConfigStore is wired, the tool still returns success but the
marker only lives in the result.metadata — single-turn-only effect.
"""
from __future__ import annotations

import dataclasses

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="EnterPlanMode",
        qualified_name="builtin.EnterPlanMode",
        description="Switch the session into plan mode (exploration + design, no destructive tools).",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    read_only=True,
)


class EnterPlanModeTool(Tool):
    def __init__(self, *, session_config_store) -> None:
        self.session_config_store = session_config_store

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        return None

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="plan-mode toggle allowed")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        if self.session_config_store is not None:
            current = await self.session_config_store.get(context.session_id)
            new_metadata = {**current.metadata, "mode": "plan"}
            updated = dataclasses.replace(current, metadata=new_metadata)
            await self.session_config_store.save(updated)
        return ToolResult(
            ok=True,
            content={"mode": "plan"},
            rendered="[EnterPlanMode] 已进入 plan 模式。后续不要执行修改性工具，专注于探索和设计。",
            metadata={"mode": "plan"},
        )

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
