"""MCP 运行配置契约:不可变 dataclass,不重复定义 pydantic 配置模型(设计 §6)。

ConfiguredServer 是合并/覆盖/展开之后的有效 server 视图:origin 记录定义来自哪一层;
enabled/enabled_tools/disabled_tools/deferred 已套用 override。enabled_tools=None 表示
全部工具,[] 表示全不选。config_error 非 None 表示该 server 凭据展开失败等配置错误,
manager 不得连接它,只展示脱敏错误。

快照整体深层只读:server 是 frozen 的 McpServer,env/headers 是只读 mapping,args 是
tuple,任何属性重赋值或嵌套修改都抛错。
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from xana.config.system_model import McpServer

if TYPE_CHECKING:
    from xana.mcp.audit import AuditFinding


@dataclass(frozen=True)
class McpCallResult:
    """一次 MCP tools/call 的协议结果(content 块为原始协议 dict,转换由 tools.py 负责)。"""

    content: tuple[dict[str, Any], ...]
    structured_content: Any
    is_error: bool


@dataclass(frozen=True)
class ConfiguredServer:
    server: McpServer
    origin: Literal["user", "project"]
    enabled: bool
    enabled_tools: frozenset[str] | None
    disabled_tools: frozenset[str]
    deferred: bool
    config_error: str | None = None   # 凭据展开失败等单 server 配置错误;非 None 时禁止连接

    def __post_init__(self) -> None:
        server = self.server
        object.__setattr__(self, "server", server.model_copy(update={
            "env": MappingProxyType(dict(server.env)),
            "headers": MappingProxyType(dict(server.headers)),
            "args": tuple(server.args),
        }))


@dataclass(frozen=True)
class McpConfigSnapshot:
    """一次成功 load 的不可变结果;load 失败时调用方继续持有上一份快照。"""

    servers: tuple[ConfiguredServer, ...]
    reconnect_initial_s: float = 2
    reconnect_max_s: float = 60
    watch_interval_s: float = 2


@dataclass(frozen=True)
class ToolView:
    """单个工具的脱敏状态视图(design §9),供 Task 5 API 投影;不含描述/schema 原文。"""

    name: str          # 限定名 mcp__{server}__{tool};被隔离时仍稳定可寻址
    raw_name: str
    status: Literal["enabled", "disabled_user", "quarantined", "unavailable"]
    fingerprint: str
    findings: tuple["AuditFinding", ...]   # rule/path/message,不含可疑原文


@dataclass(frozen=True)
class ServerView:
    """单个 server 的脱敏状态视图(design §9);不含 env/header 值与工具定义原文。"""

    name: str
    origin: Literal["user", "project"]
    transport: Literal["stdio", "http", "sse"]
    enabled: bool                # 用户选择;故障摘除不改变它
    status: Literal["connecting", "connected", "retry_waiting", "disabled", "failed", "removed"]
    config_error: str | None
    error: str | None
    retry_in_s: float | None
    tools: tuple[ToolView, ...]
