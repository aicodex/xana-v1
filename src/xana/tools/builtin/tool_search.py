"""tool_search: 揭示隐藏工具(deferred tools)的完整 schema(CC ToolSearchTool 同款)。

deferred 工具只在 system prompt 的「# 按需工具」段以名字出现;模型经本工具
查询(select:精确 / 关键词评分 / +必须词)后,匹配工具的完整 JSONSchema 以
<functions> 块返回,且名字写入 ToolResult.metadata["revealed_tools"] —— harness
将其透传进 MemoryMessage metadata,后续 turn 据此恢复完整 schema。
"""
from __future__ import annotations

import json

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_DESCRIPTION = (
    "获取隐藏工具(deferred tools)的完整 schema 定义,使其可被调用。\n"
    "隐藏工具只在「# 按需工具」段以名字出现——在获取前没有参数 schema,无法调用。"
    "本工具接收一个查询,与隐藏工具列表匹配,在 <functions> 块内返回匹配工具的完整 "
    "JSONSchema 定义。一旦某工具的 schema 出现在结果中,它就和提示顶部定义的工具一样可直接调用。\n"
    "查询形式:\n"
    '- "select:web_search,cron_create" —— 按名字精确获取\n'
    '- "网页 搜索" —— 关键词搜索,返回至多 max_results 个最佳匹配\n'
    '- "+cron 创建" —— 名字必须含 "cron",其余词参与排序'
)

_SPEC = ToolSpec(
    toolset="meta",
    component=ComponentSpec(
        kind="tool", name="tool_search", qualified_name="builtin.tool_search",
        description=_DESCRIPTION,
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": '查询。"select:<名字>" 精确选择,或关键词搜索。'},
            "max_results": {"type": "number", "description": "最多返回数(默认 5)"},
        },
        "required": ["query"],
    },
    read_only=True,
    display=ToolDisplay(category="other", verb="加载工具", summary_args=("query",)),
)


class ToolSearchTool(Tool):
    def __init__(self, *, tool_registry) -> None:
        self.tool_registry = tool_registry

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        query = str(invocation.input.get("query", "")).strip()
        if not query:
            raise ValueError("query 不能为空")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="tool_search is read-only")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        query = str(invocation.input.get("query", "")).strip()
        max_results = max(1, int(invocation.input.get("max_results", 5) or 5))
        specs = [s for s in await self.tool_registry.list_specs() if getattr(s, "defer", False)]
        by_name = {s.component.name: s for s in specs}
        if query.startswith("select:"):
            names = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
            matched = [by_name[n] for n in names if n in by_name]
        else:
            terms = query.split()
            required = [t[1:].lower() for t in terms if t.startswith("+")]
            optional = [t.lower() for t in terms if not t.startswith("+")]
            scored = []
            for s in specs:
                name = s.component.name.lower()
                desc = (s.component.description or "").lower()
                if any(r not in name for r in required):
                    continue
                score = sum(10 if t == name else 5 if t in name else 2 if t in desc else 0
                            for t in (optional or required))
                if score > 0:
                    scored.append((score, s))
            scored.sort(key=lambda x: -x[0])
            matched = [s for _, s in scored[:max_results]]
        revealed = [s.component.name for s in matched]
        if not matched:
            rendered = f"未匹配到隐藏工具(共 {len(specs)} 个可搜)。换关键词或用 select:<名字>。"
        else:
            lines = "\n".join(
                "<function>" + json.dumps(
                    {"description": s.component.description or "",
                     "name": s.component.name,
                     "parameters": dict(s.input_schema)},
                    ensure_ascii=False) + "</function>"
                for s in matched)
            rendered = f"<functions>\n{lines}\n</functions>"
        display = (f"已加载 {len(revealed)} 个工具: {', '.join(revealed)},下一轮可直接调用。"
                   if revealed else rendered)  # 未命中时的纯文本本来就友好
        # metadata 同时携带 revealed_tools(harness 持久化)与 display_text(UI 覆盖)
        return ToolResult(ok=True, content={"matches": revealed, "query": query,
                                            "total_deferred_tools": len(specs)},
                          rendered=rendered,
                          metadata={"revealed_tools": revealed, "display_text": display})
