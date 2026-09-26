"""web_search / web_open_url / background_research:调用独立部署的 xana-search 服务(~/project/xana-search)。

服务负责并行搜索(自建 SearXNG)、并行抓取、小模型按目标阅读与引用校验、深度搜索后台任务;
这里只做参数校验、带 token 的 HTTP 请求和结果渲染。地址与 token 在 system.json
web_search.xana_search_url / xana_search_token。
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Callable

import httpx

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

_OPEN_TIMEOUT_S = 180.0
_MAX_WAIT_S = 600

_SEARCH_SPEC = ToolSpec(
    toolset="web",
    component=ComponentSpec(
        kind="tool", name="web_search", qualified_name="builtin.web_search",
        description=(
            "联网搜索。一次给 1–4 条查询,并行执行、合并去重,返回每条结果的标题、链接、日期和搜索引擎摘要"
            "(不是网页全文),几秒内返回。彼此独立的搜索(不同对象、不同角度)超过 4 条时,在同一条回复里发出"
            "多个 web_search 调用,它们会并行执行。\n"
            "摘要里看起来相关、需要细节或核实的结果,用 web_open_url 打开读全文;只打开真正需要的几个。"
            "查询要具体、简短,从不同角度切入;结果里没有需要的内容就换关键词再搜。回答中引用来源用 [标题](url)。"),
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "queries": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4,
                        "description": "搜索查询,1–4 条,并行执行"},
            "recency_days": {"type": "integer", "minimum": 1, "description": "只要最近 N 天的结果(可选)"},
            "domains": {"type": "array", "items": {"type": "string"}, "maxItems": 10,
                        "description": "限定站点,如 [\"cninfo.com.cn\"](可选)"},
        },
        "required": ["queries"],
    },
    read_only=True, open_world=True, concurrency="parallel", timeout_s=60,
    display=ToolDisplay(category="web_search", verb="搜索网页", summary_args=("queries",)),
)

_OPEN_SPEC = ToolSpec(
    toolset="web",
    component=ComponentSpec(
        kind="tool", name="web_open_url", qualified_name="builtin.web_open_url",
        description=(
            "打开网页读全文(一次 1–10 个 URL,并行抓取;需要 JS 渲染的页面会用浏览器渲染,PDF 会抽文本)。"
            "目的不同的几组网页,可以在同一条回复里发出多个 web_open_url 调用(各带自己的 objective),并行执行。"
            "给了 objective 时,按它提炼要点,每条附原文引用,数字已逐字核对;不给则返回正文开头。"
            "全文保存在返回的目录里,要核对原文时用 read/grep。返回的内容像登录页或落地页,说明被登录墙挡住了。"),
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "urls": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10,
                     "description": "要打开的完整 http/https 链接,1–10 个"},
            "objective": {"type": "string", "description": "要从这些页面里找什么(可选,提炼要点的依据)"},
        },
        "required": ["urls"],
    },
    read_only=True, open_world=True, concurrency="parallel", timeout_s=_OPEN_TIMEOUT_S,
    display=ToolDisplay(category="web_open", verb="打开", unit="个网页", count_arg="urls", summary_args=("urls", "objective")),
)

_DEEP_SPEC = ToolSpec(
    toolset="research",
    component=ComponentSpec(
        kind="tool", name="background_research", qualified_name="builtin.background_research",
        description=(
            "后台研究(在搜索服务端独立运行,用本地模型规划和阅读,不占用主模型)。给一个研究问题,服务自己拆子问题、多轮检索与补搜、阅读上百个网页,最后写出带编号"
            "引用的报告。effort:low 约 3 个子问题 1 轮(几分钟);medium 4 个子问题 2 轮(默认,约 5–15 分钟);"
            "high 6 个子问题 3 轮(约 15–40 分钟,适合产业链梳理、多方交叉验证)。\n"
            "用法:提交时给 question,立即返回 job_id;之后用 job_id 查询,wait_s 表示最多等待多少秒(≤600),"
            "任务结束会提前返回。等待期间可以先做别的事。报告里的数字来自已核对的原文引用。"),
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "研究问题(提交新任务时填)"},
            "effort": {"type": "string", "enum": ["low", "medium", "high"], "description": "努力程度,默认 medium"},
            "job_id": {"type": "string", "description": "查询已提交的任务"},
            "wait_s": {"type": "integer", "minimum": 0, "maximum": _MAX_WAIT_S,
                       "description": "查询时最多等待的秒数,默认 0(立即返回当前进度)"},
            "cancel": {"type": "boolean", "description": "取消 job_id 对应的任务"},
        },
    },
    read_only=True, open_world=True, concurrency="parallel", timeout_s=_MAX_WAIT_S + 30,
    display=ToolDisplay(category="research", verb="后台研究", summary_args=("question", "job_id")),
)


class XanaSearchClient:
    """xana-search 服务的 HTTP 客户端(Bearer token)。"""

    def __init__(self, base_url: str, token: str, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._transport = transport

    async def request(self, method: str, path: str, *, json: dict | None = None,
                      params: dict | None = None, timeout: float) -> dict[str, Any]:
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(timeout, connect=10),
                                     transport=self._transport) as client:
            resp = await client.request(method, self.base_url + path, json=json, params=params,
                                        headers={"Authorization": f"Bearer {self.token}"})
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text[:300]
            raise XanaSearchError(f"HTTP {resp.status_code}: {detail}", retryable=resp.status_code >= 500)
        return resp.json()

    async def follow(self, job_id: str, *, timeout_s: float, progress: Callable[[str], None] | None,
                     cancel_on_abort: bool) -> dict[str, Any]:
        """长轮询任务直到结束或超时;每有新进度就回调 progress(给渠道显示)。
        cancel_on_abort:调用方被取消时是否一并取消服务端任务(搜索是;深度搜索只停止等待,任务继续)。"""
        deadline = time.monotonic() + timeout_s
        since = 0.0  # 首次轮询也按"有更新即返回",否则要等满一段才看到第一条进度
        last = ""
        try:
            while True:
                wait = max(1.0, min(30.0, deadline - time.monotonic()))
                params: dict[str, Any] = {"wait": wait, "since": since}
                row = await self.request("GET", f"/v1/jobs/{job_id}", params=params, timeout=wait + 30)
                since = float(row.get("updated") or since)
                msg = row.get("message") or ""
                if progress is not None and msg and msg != last and row["status"] == "running":
                    progress(msg)
                    last = msg
                if row["status"] not in ("queued", "running") or time.monotonic() >= deadline:
                    return row
        except asyncio.CancelledError:
            if not cancel_on_abort:
                raise
            with contextlib.suppress(Exception):
                await asyncio.shield(self.request("DELETE", f"/v1/jobs/{job_id}", timeout=5))
            raise


class XanaSearchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _failure(tool: str, exc: Exception) -> ToolResult:
    retryable = isinstance(exc, httpx.HTTPError) or getattr(exc, "retryable", False)
    msg = f"{type(exc).__name__}: {exc}" if isinstance(exc, httpx.HTTPError) else str(exc)
    return ToolResult(ok=False, content=None, failure_kind="execution", error=msg, retryable=retryable,
                      rendered=f"[{tool}] 搜索服务调用失败:{msg}")


class _Base(Tool):
    _spec: ToolSpec

    def __init__(self, client: XanaSearchClient) -> None:
        self.client = client

    def spec(self) -> ToolSpec:
        return self._spec

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="只读联网搜索")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext,
                            result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None


class XanaWebSearchTool(_Base):
    _spec = _SEARCH_SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not [q for q in invocation.input.get("queries") or [] if str(q).strip()]:
            raise ValueError("queries 至少一条")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        inp = invocation.input
        body: dict[str, Any] = {"queries": [str(q) for q in inp["queries"]][:4], "read": 0}
        if inp.get("recency_days"):
            body["recency_days"] = int(inp["recency_days"])
        if inp.get("domains"):
            body["domains"] = list(inp["domains"])[:10]
        try:
            data = await self.client.request("POST", "/v1/search", json=body, timeout=60)
        except (httpx.HTTPError, XanaSearchError) as exc:
            return _failure("web_search", exc)
        stats = data["report"]["stats"]
        return ToolResult(ok=True, content={"stats": stats}, rendered=data["text"],
                          metadata={"display_text": f"{stats['queries']} 个查询 · {stats['hits']} 条结果"})


class WebOpenUrlTool(_Base):
    _spec = _OPEN_SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        urls = invocation.input.get("urls") or []
        if not urls or any(not str(u).startswith(("http://", "https://")) for u in urls):
            raise ValueError("urls 需要 1–10 个完整的 http/https 链接")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        inp = invocation.input
        try:
            job = await self.client.request("POST", "/v1/fetch", timeout=30, json={
                "urls": [str(u) for u in inp["urls"]][:10], "objective": str(inp.get("objective") or ""),
                "background": True})
            row = await self.client.follow(job["job_id"], timeout_s=_OPEN_TIMEOUT_S,
                                           progress=context.report_progress, cancel_on_abort=True)
            if row["status"] != "completed":
                raise XanaSearchError(f"读取任务 {row['status']}: {row.get('error') or row.get('message')}",
                                      retryable=row["status"] == "interrupted")
        except (httpx.HTTPError, XanaSearchError) as exc:
            return _failure("web_open_url", exc)
        data = row["result"]
        stats = data["report"]["stats"]
        rel = f" · 相关 {stats['relevant']}" if stats.get("summarized") else ""
        return ToolResult(ok=True, content={"stats": stats, "dump_dir": data["report"]["dump_dir"]},
                          rendered=data["text"],
                          metadata={"display_text": f"{stats['fetched']}/{stats['read']} 个网页{rel}"})


class BackgroundResearchTool(_Base):
    _spec = _DEEP_SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        inp = invocation.input
        if bool(inp.get("question")) == bool(inp.get("job_id")):
            raise ValueError("提交新任务给 question,查询或取消给 job_id,二者只能给一个")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        inp = invocation.input
        try:
            if inp.get("question"):
                sub = await self.client.request("POST", "/v1/research", timeout=30, json={
                    "question": str(inp["question"]), "effort": str(inp.get("effort") or "medium")})
                text = (f"深度搜索已提交,job_id={sub['job_id']},预计 {sub['expected_minutes']:g} 分钟内完成。"
                        f"用 background_research(job_id=\"{sub['job_id']}\", wait_s=600) 等待结果,"
                        "等待期间可以先做别的事。")
                return ToolResult(ok=True, content=sub, rendered=text,
                                  metadata={"display_text": f"深度搜索已提交 · {sub['job_id']}"})
            job_id = str(inp["job_id"])
            if inp.get("cancel"):
                res = await self.client.request("DELETE", f"/v1/jobs/{job_id}", timeout=30)
                return ToolResult(ok=True, content=res, rendered=f"任务 {job_id} 已取消" if res.get("cancelled")
                                  else f"任务 {job_id} 已不在运行")
            wait = min(int(inp.get("wait_s") or 0), _MAX_WAIT_S)
            if wait:
                row = await self.client.follow(job_id, timeout_s=wait, progress=context.report_progress,
                                               cancel_on_abort=False)
            else:
                row = await self.client.request("GET", f"/v1/jobs/{job_id}", timeout=30)
        except (httpx.HTTPError, XanaSearchError) as exc:
            return _failure("background_research", exc)
        return ToolResult(ok=True, content={k: row.get(k) for k in ("id", "status", "phase", "counters")},
                          rendered=_render_job(row), metadata={"display_text": _job_display(row)})


def _job_display(row: dict) -> str:
    c = row.get("counters") or {}
    return (f"深度搜索 {row['status']} · {row.get('message', '')} · 查询 {c.get('queries', 0)} · "
            f"网页 {c.get('pages', 0)} · 证据 {c.get('evidence', 0)}")


def _render_job(row: dict) -> str:
    status = row["status"]
    c = row.get("counters") or {}
    if status == "completed":
        r = row["result"]
        st = r.get("stats") or {}
        head = (f"深度搜索完成:{st.get('queries', 0)} 个查询,阅读 {st.get('pages', 0)} 个网页,"
                f"证据 {st.get('evidence', 0)} 条,用时 {st.get('elapsed_s', 0):.0f} 秒。"
                f"全文与证据保存在 {r.get('dump_dir')}/(result.json 含全部证据和原文引用)。\n\n")
        gaps = "\n\n(检索说明:" + ";".join(r["gaps"]) + ")" if r.get("gaps") else ""
        return head + r["report"] + gaps
    if status in ("failed", "interrupted", "cancelled"):
        return f"深度搜索任务 {row['id']} 状态 {status}:{row.get('error') or row.get('message')}"
    recent = ";".join(e["message"] for e in (row.get("events") or [])[-5:])
    return (f"深度搜索进行中(job_id={row['id']}):{row.get('message')}。已完成查询 {c.get('queries', 0)} 个,"
            f"阅读网页 {c.get('pages', 0)} 个,证据 {c.get('evidence', 0)} 条。最近进展:{recent}。"
            f"继续用 background_research(job_id=\"{row['id']}\", wait_s=600) 等待。")
