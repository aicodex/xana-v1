"""task_list / task_wait / task_stop:查询、等待和停止独立后台任务。"""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.harness_sdk.background import BackgroundTasks
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

WAIT_MAX_S = 600

_LIST_SPEC = ToolSpec(
    temporal="live", toolset="agents", read_only=True, concurrency="parallel",
    component=ComponentSpec(kind="tool", name="task_list", qualified_name="builtin.task_list",
        description="列出本会话的后台任务、任务 ID、状态和最近进度。"
                    "默认包含运行中及结果尚未交付的任务；include_finished=true 可查看已交付记录。查询不会确认结果或停止任务。",
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={"type": "object", "properties": {
        "include_finished": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
    display=ToolDisplay(category="other", verb="查看后台任务"),
)

_WAIT_SPEC = ToolSpec(
    temporal="static",
    toolset="agents",
    component=ComponentSpec(
        kind="tool", name="task_wait", qualified_name="builtin.task_wait",
        description=(f"等待后台任务(agent_run / eval_run 转后台时返回的任务 id):任务结束就立即返回结果,"
                     f"否则最多等 wait_s 秒(≤{WAIT_MAX_S})后返回当前进度,可以再次调用继续等。"
                     "任务结束时结果会自动送到当前会话。"),
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={"type": "object", "properties": {
        "task_id": {"type": "string"},
        "wait_s": {"type": "integer", "minimum": 0, "maximum": WAIT_MAX_S, "description": f"默认 {WAIT_MAX_S}"}},
        "required": ["task_id"]},
    read_only=True, concurrency="parallel",
    display=ToolDisplay(category="other", verb="等待后台任务", summary_args=("task_id",)),
)

_STOP_SPEC = ToolSpec(
    temporal="static",
    toolset="agents",
    component=ComponentSpec(
        kind="tool", name="task_stop", qualified_name="builtin.task_stop",
        description="停止一个仍在运行的后台任务(子 agent 会被中止;已产出的进度保留在任务结果里)。",
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    display=ToolDisplay(category="other", verb="停止后台任务", summary_args=("task_id",)),
)


def _fail(name: str, msg: str) -> ToolResult:
    return ToolResult(ok=False, content=None, error=msg, failure_kind="not_found", rendered=f"[{name}] {msg}")


class TaskListTool(Tool):
    def __init__(self, background: BackgroundTasks) -> None:
        self._bg = background

    def spec(self) -> ToolSpec:
        return _LIST_SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="task_list")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        records = self._bg.list_for_session(context.session_id,
            include_finished=bool(invocation.input.get("include_finished", False)))
        limit = max(1, min(50, int(invocation.input.get("limit", 20))))
        rows = [{"task_id": r.id, "kind": r.kind, "label": r.label, "status": r.status,
                 "delivered": r.delivered, "progress": list(r.progress),
                 **({"agent_id": r.content["agent_id"]} if r.content.get("agent_id") else {})}
                for r in records[:limit]]
        lines = [f'{r["task_id"]} · {r["status"]} · {r["label"]}' +
                 (f' · {r["progress"][-1]}' if r["progress"] else "") for r in rows]
        return ToolResult(True, {"tasks": rows, "total": len(records)},
                          rendered="\n".join(lines) or "本会话没有运行中或待交付的后台任务。")


class TaskWaitTool(Tool):
    def __init__(self, background: BackgroundTasks) -> None:
        self._bg = background

    def spec(self) -> ToolSpec:
        return _WAIT_SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="task_wait")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        task_id = str(invocation.input["task_id"])
        if self._bg.get(task_id, session_id=context.session_id) is None:
            return _fail("task_wait", f"没有后台任务 {task_id}")
        wait_s = min(int(invocation.input.get("wait_s", WAIT_MAX_S)), WAIT_MAX_S)
        rec = await self._bg.wait(task_id, wait_s, wakeup=context.input_wakeup)
        if rec.status == "running":
            return ToolResult(ok=True, content={"task_id": task_id, "status": "running"}, rendered=(
                f"[task_wait] {rec.summary()},仍在运行。最近进度:\n" + ("\n".join(rec.progress) or "(无)")))
        return ToolResult(ok=rec.status == "completed", content={"task_id": task_id, "status": rec.status, **rec.content},
                          background_task_id=rec.id,
                          error=None if rec.status == "completed" else rec.status,
                          failure_kind=None if rec.status == "completed" else "execution",
                          rendered=f"[task_wait] {rec.summary()}\n{rec.rendered}")


class TaskStopTool(Tool):
    def __init__(self, background: BackgroundTasks) -> None:
        self._bg = background

    def spec(self) -> ToolSpec:
        return _STOP_SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="task_stop")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        task_id = str(invocation.input["task_id"])
        if self._bg.get(task_id, session_id=context.session_id) is None:
            return _fail("task_stop", f"没有后台任务 {task_id}")
        stopped = await self._bg.stop(task_id)
        return ToolResult(ok=True, content={"task_id": task_id, "stopped": stopped},
                          rendered=f"[task_stop] {'已停止' if stopped else '任务已经结束'} {task_id}")
