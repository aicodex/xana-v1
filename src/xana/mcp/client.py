"""官方 MCP SDK 连接适配(docs/superpowers/specs/2026-11-13-mcp-design.md §6)。

每个 McpClientConnection 拥有独立 AsyncExitStack,按 transport 封装 stdio /
streamable-http / sse、initialize、tools/list 全分页、notifications/tools/list_changed、
ping 与完整关闭。只有传输层失败(断流、读写超时、进程/HTTP 连接错误)被翻译为
McpTransportError;协议错误响应(McpError)与编程错误原样上抛,由 manager 分类。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from typing import Any

import anyio
import httpx
import mcp.types as types
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError

from xana.mcp.contracts import ConfiguredServer, McpCallResult


class McpTransportError(Exception):
    """MCP 传输层失败:连接中断、读写超时、子进程/HTTP 连接错误。"""


# 超时与连接关闭在 SDK 内以 McpError(特定 code)上报,其余传输错误为底层异常类型。
_TRANSPORT_MCP_CODES = {httpx.codes.REQUEST_TIMEOUT, types.CONNECTION_CLOSED}
_TRANSPORT_ERRORS = (
    httpx.HTTPError,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
    ConnectionError,
    OSError,
    TimeoutError,
    asyncio.TimeoutError,
)


def _translate(exc: BaseException) -> BaseException:
    """把 SDK 异常归一化为传输/非传输两类;返回应向上抛的异常。"""
    if isinstance(exc, McpError):
        if exc.error.code in _TRANSPORT_MCP_CODES:
            return McpTransportError(exc.error.message)
        return exc    # 协议错误响应:交给 manager 判为 failed,不判为断连
    if isinstance(exc, _TRANSPORT_ERRORS):
        return McpTransportError(str(exc) or type(exc).__name__)
    return exc        # 编程错误原样上抛


class _ManagedSession(ClientSession):
    """内嵌 SDK session:接收循环意外结束触发 on_closed;tools/list_changed 回调 manager。"""

    def __init__(self, read_stream: Any, write_stream: Any, *,
                 on_tools_changed: Callable[[], None],
                 on_closed: Callable[[BaseException | None], None],
                 read_timeout_seconds: timedelta | None = None) -> None:
        super().__init__(read_stream, write_stream,
                         read_timeout_seconds=read_timeout_seconds)
        self._on_tools_changed = on_tools_changed
        self._on_closed = on_closed
        self.client_closed = False

    async def _received_notification(self, notification: types.ServerNotification) -> None:
        root = notification.root if hasattr(notification, "root") else notification
        if isinstance(root, types.ToolListChangedNotification):
            self._on_tools_changed()
        await super()._received_notification(notification)

    async def _receive_loop(self) -> None:
        try:
            await super()._receive_loop()
        finally:
            if not self.client_closed:
                self._on_closed(None)


class McpClientConnection:
    """一条已初始化的 MCP 连接;持有 SDK session 与独立 AsyncExitStack。"""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._session: _ManagedSession | None = None
        self._closed = False

    # ---- 建立 ----

    @classmethod
    async def connect(
        cls,
        config: ConfiguredServer,
        *,
        environ: Mapping[str, str],
        on_tools_changed: Callable[[], None],
        on_closed: Callable[[BaseException | None], None],
    ) -> tuple["McpClientConnection", tuple[dict[str, Any], ...]]:
        """按 transport 建立连接、initialize 并返回首个 tools/list 全量结果。

        实现跑在独立任务里并经 shield 观察:SDK 建连失败的清理(anyio task group /
        exit stack)会向实现任务注入内部 CancelledError(如 streamable-http 连接被拒),
        一律按传输失败翻译;调用方被外部取消(serve/manager close)时原样传播。
        """
        task = asyncio.create_task(cls._connect_impl(
            config, environ=environ,
            on_tools_changed=on_tools_changed, on_closed=on_closed))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise McpTransportError("连接建立被中断") from None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    @classmethod
    async def _connect_impl(
        cls,
        config: ConfiguredServer,
        *,
        environ: Mapping[str, str],
        on_tools_changed: Callable[[], None],
        on_closed: Callable[[BaseException | None], None],
    ) -> tuple["McpClientConnection", tuple[dict[str, Any], ...]]:
        server = config.server
        self = cls()
        try:
            if server.transport == "stdio":
                env = {**dict(environ), **dict(server.env)} or None
                params = StdioServerParameters(
                    command=server.command, args=list(server.args),
                    env=env, cwd=server.cwd,
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
            elif server.transport == "http":
                read, write, _ = await self._stack.enter_async_context(
                    streamablehttp_client(server.url,
                                          headers=dict(server.headers) or None))
            else:
                read, write = await self._stack.enter_async_context(
                    sse_client(server.url, headers=dict(server.headers) or None))
            self._session = await self._stack.enter_async_context(_ManagedSession(
                read, write,
                on_tools_changed=on_tools_changed, on_closed=on_closed,
                read_timeout_seconds=timedelta(seconds=max(server.tool_timeout_s, 1.0)),
            ))
            await asyncio.wait_for(self._session.initialize(),
                                   timeout=max(server.startup_timeout_s, 0.1))
            tools = await self.list_tools()
        except Exception as exc:
            await self._stack.aclose()
            raise _translate(exc) from exc
        except asyncio.CancelledError as exc:
            # 内部取消(建连失败清理路径):按传输失败翻译;外部取消不经过这里(shield)。
            with suppress(BaseException):
                await self._stack.aclose()
            raise McpTransportError("连接建立被中断") from exc
        return self, tools

    # ---- 协议操作(传输失败一律翻译为 McpTransportError) ----

    async def list_tools(self) -> tuple[dict[str, Any], ...]:
        """耗尽 tools/list 全部游标分页;任何一页失败都抛错,调用方不得拿部分列表发布。"""
        assert self._session is not None
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        try:
            while True:
                page = await self._session.list_tools(cursor=cursor)
                out.extend(t.model_dump(mode="json", by_alias=True, exclude_none=True)
                           for t in page.tools)
                cursor = page.nextCursor
                if not cursor:
                    return tuple(out)
        except Exception as exc:
            raise _translate(exc) from exc

    async def call_tool(self, name: str, arguments: dict[str, Any],
                        timeout_s: float) -> McpCallResult:
        assert self._session is not None
        try:
            result = await self._session.call_tool(
                name, dict(arguments),
                read_timeout_seconds=timedelta(seconds=max(timeout_s, 0.1)),
            )
        except Exception as exc:
            raise _translate(exc) from exc
        return McpCallResult(
            content=tuple(b.model_dump(mode="json", by_alias=True, exclude_none=True)
                          for b in result.content),
            structured_content=result.structuredContent,
            is_error=bool(result.isError),
        )

    async def ping(self) -> None:
        assert self._session is not None
        try:
            await self._session.send_ping()
        except Exception as exc:
            raise _translate(exc) from exc

    # ---- 关闭 ----

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._session is not None:
            self._session.client_closed = True    # 抑制 on_closed 回调
        await self._stack.aclose()
