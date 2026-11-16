"""MCP 工具适配(docs/superpowers/specs/2026-11-13-mcp-design.md §7):已审核的工具定义
→ ToolSpec / McpTool / McpCallResult → ToolResult 转换。

- annotations 保守映射:只有显式 readOnlyHint=True 才只读;destructiveHint 未声明时按
  非只读保守处理;openWorldHint 缺省 True(§7.2)。
- check_permission 只声明 allow;manual/yolo/auto、plan mode 等策略由 PermissionService
  在统一执行链上应用,McpTool 不开额外后门。
- 结果转换:文本合并;structuredContent 写入 structured_content;图片经 FileMediaStore
  校验落盘为 MediaInput;embedded text resource 作为文本;resource link 只给链接说明,
  不主动抓取;音频与未知二进制块明确未支持,不伪装成功(§7.3)。
- 传输异常(McpTransportError)触发 on_transport_failure(管理器据此摘除当前 generation
  全部工具)并使本次调用返回 unsupported + retryable;不自动重试、不在新连接上重放;
  编程错误原样上抛,不触发失效/重连。
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from xana.component.contracts import ComponentSpec, ComponentSource
from xana.mcp.client import McpTransportError
from xana.mcp.contracts import ConfiguredServer, McpCallResult
from xana.multimodal.contracts import MediaInput
from xana.multimodal.files import FileMediaStore
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

MCP_TOOLSET = "mcp"

_DEFAULT_TOOL_TIMEOUT_S = 60.0


class McpCaller(Protocol):
    """McpTool 需要的连接面(Task 4 的具体连接对象结构满足此协议)。"""

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: float) -> McpCallResult: ...


def build_tool_spec(config: ConfiguredServer, raw: Mapping[str, Any], qualified_name: str) -> ToolSpec:
    """已审核的 MCP 工具定义 → ToolSpec。registry 名用限定名;server 侧原始名记入 metadata 供调用。"""
    server = config.server
    annotations = raw.get("annotations")
    if not isinstance(annotations, Mapping):
        annotations = {}
    read_only = annotations.get("readOnlyHint") is True
    destructive_hint = annotations.get("destructiveHint")
    destructive = (not read_only) if destructive_hint is None else destructive_hint is True
    open_world = annotations.get("openWorldHint", True)
    if not isinstance(open_world, bool):
        open_world = True
    input_schema = raw.get("inputSchema")
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object"}
    return ToolSpec(
        component=ComponentSpec(
            kind="tool",
            name=qualified_name,
            qualified_name=qualified_name,
            description=str(raw.get("description") or ""),
            source=ComponentSource(source_type="mcp", scope=config.origin),
            metadata={"mcp_server": server.name, "mcp_tool": str(raw.get("name") or "")},
        ),
        input_schema=input_schema,
        read_only=read_only,
        destructive=destructive,
        open_world=open_world,
        concurrency="parallel" if read_only else "serialized",
        timeout_s=server.tool_timeout_s,
        defer=config.deferred,
        toolset=MCP_TOOLSET,
    )


class McpTool(Tool):
    """一个已发布的 MCP 工具。connection 由管理器注入并随 generation 更换。"""

    def __init__(self, spec: ToolSpec, connection: McpCaller,
                 on_transport_failure: Callable[[], None], is_current: Callable[[], bool],
                 media_store: FileMediaStore) -> None:
        self._spec = spec
        self._connection = connection
        self._on_transport_failure = on_transport_failure
        self._is_current = is_current
        self._media_store = media_store
        tool = spec.component.metadata.get("mcp_tool")
        self._tool_name = str(tool) if tool else spec.component.name

    def spec(self) -> ToolSpec:
        return self._spec

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="mcp tool declared allow; policy via PermissionService")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        if not self._is_current():
            return ToolResult(ok=False, content=None,
                              error=f"MCP 工具 {self._spec.component.name} 已失效(连接 generation 已变化)",
                              failure_kind="unsupported", retryable=True,
                              rendered=f"[{self._spec.component.name}] 工具已失效,请重试")
        try:
            result = await self._connection.call_tool(
                self._tool_name, dict(invocation.input),
                self._spec.timeout_s or _DEFAULT_TOOL_TIMEOUT_S)
        except McpTransportError as exc:    # 仅传输层失败:通知管理器摘除本 generation
            self._on_transport_failure()    # 编程错误原样上抛,不触发失效/重连
            return ToolResult(ok=False, content=None, error=f"MCP 传输错误:{exc}",
                              failure_kind="unsupported", retryable=True,
                              rendered=f"[{self._spec.component.name}] 传输错误:{exc}")
        return convert_mcp_result(result, self._media_store)


def convert_mcp_result(result: McpCallResult, media_store: FileMediaStore) -> ToolResult:
    """MCP 协议结果 → ToolResult。内容块逐个归一化;未支持块返回明确失败,不伪装成功交付。"""
    texts: list[str] = []
    media: list[MediaInput] = []
    for block in result.content:
        if not isinstance(block, Mapping):
            return _unsupported(f"无法识别的 MCP 内容块:{block!r}")
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text") or ""))
        elif kind == "image":
            saved = _save_image(block, media_store)
            if isinstance(saved, str):
                return _unsupported(saved)
            media.append(saved)
        elif kind == "resource":
            resource = block.get("resource")
            if isinstance(resource, Mapping) and resource.get("text") is not None:
                texts.append(str(resource["text"]))
            else:
                return _unsupported("不支持非文本的 embedded resource(首版仅支持文本内嵌)")
        elif kind == "resource_link":
            uri = str(block.get("uri") or "")
            texts.append(f"[resource link] {uri}(不主动访问;需要内容请用允许的工具自行读取)")
        else:
            return _unsupported(f"不支持的 MCP 内容块类型:{kind!r}(音频与未知二进制块首版不支持)")
    structured = result.structured_content if isinstance(result.structured_content, dict) else None
    text = "\n".join(t for t in texts if t)
    if result.is_error:     # server 执行出错:映射为工具失败,不据此判定断连、不摘除 server
        return ToolResult(ok=False, content=text or None, structured_content=structured,
                          error=text or "MCP 工具返回 isError=true", failure_kind="execution",
                          rendered=text or None)
    return ToolResult(ok=True, content=text or None, structured_content=structured,
                      rendered=text or None, media=tuple(media))


def _save_image(block: Mapping[str, Any], media_store: FileMediaStore) -> MediaInput | str:
    data = block.get("data")
    if not isinstance(data, str):
        return "图片内容块缺少 base64 data"
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return "图片内容块 data 不是合法 base64"
    try:
        return media_store.save_bytes(raw)
    except ValueError as exc:
        return f"图片内容块无法保存:{exc}"


def _unsupported(reason: str) -> ToolResult:
    return ToolResult(ok=False, content=None, error=reason, failure_kind="unsupported",
                      rendered=f"[mcp] {reason}")
