"""MCP 支持:配置契约与运行期配置读取/写入、SDK 连接、审核、工具发布状态机
(docs/superpowers/specs/2026-11-13-mcp-design.md)。

contracts 是不可变 dataclass 运行视图;config 是 McpConfigStore 与凭据展开;
client 是官方 SDK 连接适配;manager 是唯一连接与发布状态机。无隐藏全局单例,
统一由 boot() 构造注入。
"""
from xana.mcp.client import McpClientConnection, McpTransportError
from xana.mcp.config import McpConfigStore, expand_environment
from xana.mcp.contracts import (
    ConfiguredServer,
    McpCallResult,
    McpConfigSnapshot,
    ServerView,
    ToolView,
)
from xana.mcp.manager import McpManager

__all__ = [
    "ConfiguredServer",
    "McpCallResult",
    "McpClientConnection",
    "McpConfigSnapshot",
    "McpConfigStore",
    "McpManager",
    "McpTransportError",
    "ServerView",
    "ToolView",
    "expand_environment",
]
