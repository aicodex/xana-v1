"""web_fetch — 抓取 URL 并抽取可读正文(deferred)。

执行逻辑同构 yuxu_backup web_fetch/handler.py:
  * Chrome UA + Accept-Language zh-CN,follow_redirects(≤10)。
  * readable 抽取:bs4 decompose script/style/nav/footer/header/aside/
    noscript/svg/form/iframe 后 get_text;无 bs4 回退正则去标签。
  * 标题取 <title>;超长头尾各半截断并标 truncated。
"""
from __future__ import annotations

import re
from typing import Any

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_STRIP_TAGS_RE = re.compile(
    r"<(script|style|nav|footer|header|aside|noscript|svg|form|iframe)\b[^>]*>"
    r".*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool", name="web_fetch", qualified_name="builtin.web_fetch",
        description=(
            "抓取指定 URL 的内容并抽取可读正文。返回标题、状态码与正文文本"
            "(超长截断,保留首尾)。URL 必须是完整合法的 http/https 地址;"
            "重定向自动跟随(至多 10 次)。本工具只读,不修改任何文件。"),
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string",
                    "description": "要抓取的完整 http/https URL"},
            "timeout": {"type": "number",
                        "description": "请求超时秒数(默认 10)"},
            "max_chars": {"type": "number",
                          "description": "正文最大字符数,超出则头尾各半截断(默认 20000)"},
            "extract": {"type": "string",
                        "enum": ["readable", "html", "both"],
                        "description": "抽取模式:readable=可读正文(默认),"
                                       "html=原始 HTML,both=两者都要"},
        },
        "required": ["url"],
    },
    read_only=True,
    open_world=True,
    defer=True,
)


def _readable_text(html: str) -> str:
    """可读正文抽取(yuxu _extract_readable 同构):bs4 优先,缺失回退正则。"""
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header",
                         "aside", "noscript", "svg", "form", "iframe"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return _WHITESPACE_RE.sub(" ", text).strip()
    except Exception:
        pass
    stripped = _STRIP_TAGS_RE.sub("", html)
    text = _TAG_RE.sub(" ", stripped)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return _WHITESPACE_RE.sub(" ", _TAG_RE.sub(" ", m.group(1))).strip()


class WebFetchTool(Tool):
    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        url = str(invocation.input.get("url", "")).strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(f"url 必须是 http/https 地址: {url!r}")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="只读网页抓取")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        url = str(invocation.input.get("url", "")).strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return ToolResult(ok=False, content=None, failure_kind="validation",
                              error=f"url 必须是 http/https 地址: {url!r}",
                              rendered=f"[web_fetch 失败: url 必须是 http/https 地址: {url!r}]")
        timeout = float(invocation.input.get("timeout", 10) or 10)
        max_chars = max(2, int(invocation.input.get("max_chars", 20000) or 20000))
        extract = str(invocation.input.get("extract", "readable") or "readable").lower()
        if extract not in {"readable", "html", "both"}:
            return ToolResult(ok=False, content=None, failure_kind="validation",
                              error=f"extract={extract!r} 非法(readable/html/both)",
                              rendered=f"[web_fetch 失败: extract={extract!r} 非法]")
        try:
            final_url, status, content_type, html = await self._fetch(url, timeout)
        except Exception as exc:
            too_many = type(exc).__name__ == "TooManyRedirects"
            return ToolResult(ok=False, content=None, failure_kind="execution",
                              error="too many redirects (>10)" if too_many
                              else f"{type(exc).__name__}: {exc}",
                              retryable=not too_many,
                              rendered=f"[web_fetch] 抓取失败: {exc}")
        ct = content_type.lower()
        if ct and "html" not in ct and "text" not in ct and "json" not in ct and "xml" not in ct:
            return ToolResult(ok=False, content=None, failure_kind="unsupported",
                              error=f"不支持的内容类型: {content_type}",
                              rendered=f"[web_fetch] 不支持的内容类型: {content_type}")
        title = _extract_title(html)
        truncated = False
        content: dict[str, Any] = {"url": final_url, "status": status,
                                   "content_type": content_type, "title": title}
        text = ""
        if extract in {"readable", "both"}:
            text = _readable_text(html)
            if len(text) > max_chars:
                half = max_chars // 2
                text = text[:half] + "\n\n[...截断...]\n\n" + text[-half:]
                truncated = True
            content["text"] = text
        if extract in {"html", "both"}:
            raw = html
            if len(raw) > max_chars * 3:
                half = (max_chars * 3) // 2
                raw = raw[:half] + "\n\n<!--截断-->\n\n" + raw[-half:]
                truncated = True
            content["html"] = raw
        content["truncated"] = truncated
        body = text if extract in {"readable", "both"} else content.get("html", "")
        rendered = (f"{title}\n{body}" if title else body)[:_SPEC.max_result_chars]
        return ToolResult(ok=True, content=content, truncated=truncated, rendered=rendered)

    async def _fetch(self, url: str, timeout: float) -> tuple[str, int, str, str]:
        import httpx
        headers = {
            "User-Agent": _DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     max_redirects=10, headers=headers) as client:
            r = await client.get(url)
        try:
            body = r.text
        except Exception:
            body = r.content.decode("utf-8", errors="replace")
        return str(r.url), r.status_code, r.headers.get("content-type", ""), body

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
