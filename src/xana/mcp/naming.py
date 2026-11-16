"""MCP 工具名清洗与限定名生成,规则对齐 Kimi `mcpCore/tool-naming.ts`。

`mcp__{server}__{tool}`;server/tool 各自清洗:非 `[A-Za-z0-9_-]` 逐字符转 `_`,
再压缩连续下划线;整体超长截断并追加稳定 8 位 SHA-256 hash,保持唯一可寻址且
长度固定。清洗是纯函数,冲突与重名隔离由 manager 负责。
"""
from __future__ import annotations

import hashlib

_PREFIX = "mcp"
_SEPARATOR = "__"
_MAX_LEN = 64
_HASH_LEN = 8


def _sanitize(part: str) -> str:
    cleaned = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in "_-")) else "_" for ch in part
    )
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned


def qualified_tool_name(server_name: str, tool_name: str) -> str:
    """返回稳定限定的注册名;同输入永远同输出,清洗不依赖外部状态。"""
    base = f"{_PREFIX}{_SEPARATOR}{_sanitize(server_name)}{_SEPARATOR}{_sanitize(tool_name)}"
    if len(base) <= _MAX_LEN:
        return base
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:_HASH_LEN]
    return f"{base[: _MAX_LEN - _HASH_LEN - 1]}_{digest}"
