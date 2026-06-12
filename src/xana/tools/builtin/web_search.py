"""web_search — 联网搜索工具(deferred;native/brave/duckduckgo)。

执行逻辑同构 yuxu_backup web_search/handler.py:
  * provider 走 SessionConfig.search_provider(/search_provider 命令切换)。
  * native(None/空/"native")= 模型端 web_search_20250305 server tool,
    与本函数工具互斥(harness 在 deepseek + native 时剔除本工具并在
    ModelRequest.metadata 标 web_search_native);若仍被调到,返回引导性错误。
  * brave:GET https://api.search.brave.com/res/v1/web/search,
    X-Subscription-Token 鉴权;无 key 时回退 duckduckgo。
  * duckduckgo:ddgs(回退 duckduckgo_search)同步库,asyncio.to_thread 包装。
"""
from __future__ import annotations

import asyncio
from typing import Any

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool", name="web_search", qualified_name="builtin.web_search",
        description=(
            "联网搜索。输入查询词,返回 {title, url, snippet} 结果列表。"
            "provider 由 /search_provider 会话设置决定(native/brave/duckduckgo)。"),
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索查询词"},
            "max_results": {"type": "number",
                            "description": "最多返回条数(默认 10,上限 20)"},
        },
        "required": ["query"],
    },
    read_only=True,
    open_world=True,
    defer=True,
)


class WebSearchTool(Tool):
    def __init__(self, *, session_config_store, brave_api_key: str | None = None) -> None:
        self.session_config_store = session_config_store
        self.brave_api_key = brave_api_key

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        return None

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="只读联网搜索")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        query = str(invocation.input.get("query", "")).strip()
        max_results = min(int(invocation.input.get("max_results", 10) or 10), 20)
        cfg = await self.session_config_store.get(context.session_id)
        provider = (cfg.search_provider or "native").lower()
        if provider == "native":
            return ToolResult(
                ok=False, content=None, failure_kind="unsupported",
                error="search_provider=native 时搜索由模型端 web_search server tool 完成,"
                      "函数工具不可用;用 /search_provider 切到 brave 或 duckduckgo。",
                rendered="[web_search] native 模式下请直接依赖模型内置搜索。")
        try:
            if provider == "brave" and self.brave_api_key:
                data = await self._http_get_json(
                    _BRAVE_ENDPOINT, headers={"X-Subscription-Token": self.brave_api_key,
                                              "Accept-Encoding": "gzip"},
                    params={"q": query, "count": max_results})
                results = [{"title": r.get("title", ""), "url": r.get("url", ""),
                            "snippet": r.get("description", "")}
                           for r in data.get("web", {}).get("results", [])]
                used = "brave"
            else:
                results = await asyncio.to_thread(self._duckduckgo, query, max_results)
                used = "duckduckgo"
        except Exception as exc:  # 网络/上游错误 → 结构化失败,不抛给 harness
            return ToolResult(ok=False, content=None, failure_kind="execution",
                              error=f"{type(exc).__name__}: {exc}", retryable=True,
                              rendered=f"[web_search] 搜索失败: {exc}")
        rendered = "\n".join(f"- {r['title']}\n  {r['url']}\n  {r['snippet']}" for r in results) \
                   or "(无结果)"
        return ToolResult(ok=True, content={"provider": used, "results": results},
                          rendered=rendered)

    async def _http_get_json(self, url: str, *, headers: dict[str, str],
                             params: dict[str, Any]) -> dict[str, Any]:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"Accept": "application/json", **headers},
                                    params=params)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _duckduckgo(query: str, max_results: int) -> list[dict[str, str]]:
        try:
            from ddgs import DDGS
        except Exception:  # pragma: no cover - 环境回退
            from duckduckgo_search import DDGS  # type: ignore
        with DDGS() as ddgs:
            items = list(ddgs.text(query, max_results=max_results))
        return [{"title": it.get("title", ""),
                 "url": it.get("href", it.get("url", "")),
                 "snippet": it.get("body", it.get("snippet", ""))}
                for it in items[:max_results]]

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
