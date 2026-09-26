"""TodoWrite: per-session task list management.

Mirrors cc's TodoWriteTool semantics. Each call OVERWRITES the session's
list; the model is responsible for sending the full snapshot every time.
We persist via TodoStore so the list survives REPL restarts.
"""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.builtin._todo_store import TodoItem, TodoStore
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_VALID_STATUS = {"pending", "in_progress", "completed"}

_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="TodoWrite",
        qualified_name="builtin.TodoWrite",
        description="Create/replace the session's structured todo list. Send the full list every call.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content":     {"type": "string"},
                        "active_form": {"type": "string"},
                        "status":      {"type": "string", "enum": sorted(_VALID_STATUS)},
                    },
                    "required": ["content", "active_form", "status"],
                },
            },
        },
        "required": ["todos"],
    },
    destructive=True,
    display=ToolDisplay(category="plan", verb="更新清单", result="none"),
)


class TodoWriteTool(Tool):
    def __init__(self, store: TodoStore) -> None:
        self.store = store

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        todos = invocation.input.get("todos")
        if not isinstance(todos, list):
            raise ValueError("TodoWrite: 'todos' must be a list")
        for i, t in enumerate(todos):
            if not isinstance(t, dict):
                raise ValueError(f"TodoWrite: item {i} must be an object")
            if t.get("status") not in _VALID_STATUS:
                raise ValueError(f"TodoWrite: item {i} has invalid status {t.get('status')!r}")
            for k in ("content", "active_form"):
                if not isinstance(t.get(k), str) or not t[k]:
                    raise ValueError(f"TodoWrite: item {i} missing/empty '{k}'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="todo write allowed")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        items = tuple(
            TodoItem(content=t["content"], active_form=t["active_form"], status=t["status"])
            for t in invocation.input["todos"]
        )
        self.store.save(context.session_id, items)
        counts = {s: sum(1 for i in items if i.status == s) for s in _VALID_STATUS}
        rendered = (
            f"[TodoWrite ok] {len(items)} item(s): "
            f"{counts['in_progress']} in-progress, "
            f"{counts['pending']} pending, "
            f"{counts['completed']} completed"
        )
        return ToolResult(ok=True, content=[dict(c=i.content, s=i.status) for i in items], rendered=rendered)

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
