"""memory_search: model-side L2 query against the shared MemorySearchEngine."""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.memory.contracts import SearchQuery
from xana.memory.interfaces import MemorySearchEngine
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="memory_search",
        qualified_name="builtin.memory_search",
        description="Search the project memory tree.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
    read_only=True,
    display=ToolDisplay(category="memory", verb="查记忆", summary_args=("query",)),
)


class MemorySearchTool(Tool):
    def __init__(self, engine: MemorySearchEngine) -> None:
        self.engine = engine

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        query = invocation.input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("memory_search requires non-empty 'query'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="read-only memory query")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        query_text = str(invocation.input.get("query", ""))
        top_k = int(invocation.input.get("top_k", 5))
        hits = await self.engine.search(SearchQuery(text=query_text, top_k=top_k))
        lines = [f"- {hit.node_path} (score={hit.score:.2f})" for hit in hits]
        rendered = "\n".join(lines) if lines else "(no hits)"
        return ToolResult(
            ok=True,
            content=[{"node_path": h.node_path, "score": h.score, "matched_spans": list(h.matched_spans)} for h in hits],
            rendered=rendered,
        )

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
