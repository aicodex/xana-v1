"""MCP 配置读取、原子项目写入与凭据展开(docs/superpowers/specs/2026-11-13-mcp-design.md §5、§10)。

McpConfigStore 只重读用户/项目配置文件的 mcp 段,复用 config 模块的 merge_mcp_configs
(与 boot 的 merge_system_config 同一合并规则),不改变其他配置 read-once 语义。写入经
update_project_config 原子完成。load 返回不可变 McpConfigSnapshot;读取失败时调用方保留
上一份快照。

凭据展开(§5.3):env/header 值支持 ``${ENV_NAME}`` 从给定 environ(缺省 serve 进程环境)
展开。未设置的变量与遗留 ``${secret:...}`` 引用只使该 server 进入 ``config_error``
(其余 server 正常加载,manager 绝不连接它),错误信息只含变量名与字段名,
不回显任何值;坏 JSON/坏 schema 仍整体拒绝,调用方保留上一份快照。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal, Mapping

from xana.config.project_config import (
    merge_mcp_configs,
    read_project_config,
    update_project_config,
)
from xana.config.system_model import Mcp, McpServer, McpServerOverride
from xana.mcp.contracts import ConfiguredServer, McpConfigSnapshot

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRET_REF = re.compile(r"\$\{secret:[^}]*\}")


def expand_environment(values: Mapping[str, str], *, environ: Mapping[str, str]) -> dict[str, str]:
    """展开 env/header 值中的 ${ENV_NAME};未设置变量与 ${secret:...} 抛 ValueError。

    错误只包含变量名/字段名,绝不包含值或 ${secret:...} 引用内容。
    """
    out: dict[str, str] = {}
    for field, value in values.items():
        if _SECRET_REF.search(value):
            raise ValueError(
                f"字段 {field!r} 使用已不支持的 ${{secret:...}} 引用;请改为 ${{ENV_NAME}} 环境变量引用"
            )

        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in environ:
                raise ValueError(f"字段 {field!r} 引用的环境变量 {name} 未设置")
            return environ[name]

        out[field] = _ENV_REF.sub(repl, value)
    return out


class McpConfigStore:
    """用户 system.json + 项目 config.json 的 mcp 段读取与项目层写入。

    workspace 是 stdio server 未写 cwd 时的生效回落(§5.1);None 表示不回落。
    """

    def __init__(self, system_config_path: Path, project_config_path: Path,
                 environ: Mapping[str, str] | None = None,
                 workspace: Path | None = None) -> None:
        self._system_config_path = Path(system_config_path)
        self._project_config_path = Path(project_config_path)
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        # stdio server 未写 cwd 时的唯一回落(设计 §5.1:用项目 workspace);None 表示不回落
        self._workspace = None if workspace is None else str(Path(workspace))

    @property
    def audit_store_path(self) -> Path:
        """项目私有 mcp/audit.jsonl 位置(design §6),manager 默认据此构造 AuditStore。"""
        return self._project_config_path.parent / "mcp" / "audit.jsonl"

    # ---- 读取 ----

    def versions(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """(mtime_ns, size) 版本号,供热加载轮询比较;文件缺失为 (0, 0)。"""
        return (
            _file_version(self._system_config_path),
            _file_version(self._project_config_path),
        )

    def load(self) -> McpConfigSnapshot:
        """分层合并并校验 mcp 段,展开凭据,返回不可变快照。

        坏 JSON/坏 schema 抛错(调用方保留上一份快照);单 server 凭据展开失败
        只把该 server 标记为 config_error,不阻塞其余 server。
        """
        user_raw = _read_mcp_section(self._system_config_path)
        project_raw = read_project_config(self._project_config_path).get("mcp") or {}
        merged = Mcp(**merge_mcp_configs(user_raw, project_raw))
        project_names = {s["name"] for s in project_raw.get("servers") or []}
        servers = tuple(
            self._configured(server, merged.overrides.get(server.name),
                             origin="project" if server.name in project_names else "user")
            for server in merged.servers
        )
        return McpConfigSnapshot(
            servers=servers,
            reconnect_initial_s=merged.reconnect_initial_s,
            reconnect_max_s=merged.reconnect_max_s,
            watch_interval_s=merged.watch_interval_s,
        )

    def _configured(self, server: McpServer, override: McpServerOverride | None,
                    origin: Literal["user", "project"]) -> ConfiguredServer:
        config_error: str | None = None
        try:
            env = expand_environment(server.env, environ=self._environ)
            headers = expand_environment(server.headers, environ=self._environ)
        except ValueError as exc:
            # 单 server 凭据展开失败只隔离该 server;env/headers 保持未展开原状,
            # manager 依据 config_error 绝不把含占位符的值发给子进程或远端。
            config_error = f"mcp server {server.name!r}: {exc}"
            env, headers = server.env, server.headers
        if env != server.env or headers != server.headers:
            server = server.model_copy(update={"env": env, "headers": headers})
        if server.transport == "stdio" and server.cwd is None and self._workspace is not None:
            server = server.model_copy(update={"cwd": self._workspace})
        enabled = override.enabled if override is not None and override.enabled is not None else server.enabled
        enabled_tools = (
            frozenset(override.enabled_tools)
            if override is not None and override.enabled_tools is not None
            else None
        )
        disabled_tools = (
            frozenset(override.disabled_tools)
            if override is not None and override.disabled_tools is not None
            else frozenset()
        )
        deferred = override.deferred if override is not None and override.deferred is not None else server.deferred
        return ConfiguredServer(
            server=server, origin=origin, enabled=enabled,
            enabled_tools=enabled_tools, disabled_tools=disabled_tools, deferred=deferred,
            config_error=config_error,
        )

    # ---- 项目层写入(均经 update_project_config 原子写) ----

    def write_project_server(self, server: McpServer) -> None:
        """注册/替换项目级 server 定义;同名用户级定义被项目层遮蔽。"""
        data = read_project_config(self._project_config_path)
        mcp = dict(data.get("mcp") or {})
        servers = [s for s in mcp.get("servers") or [] if s.get("name") != server.name]
        servers.append(server.model_dump(exclude_none=True))
        mcp["servers"] = servers
        update_project_config(self._project_config_path, mcp=mcp)

    def delete_project_server(self, name: str) -> None:
        """一次原子写同时删除项目 server 定义与其项目 override;同名用户 server 恢复生效。"""
        data = read_project_config(self._project_config_path)
        mcp = dict(data.get("mcp") or {})
        old_servers = mcp.get("servers") or []
        old_overrides = mcp.get("overrides") or {}
        servers = [s for s in old_servers if s.get("name") != name]
        overrides = {k: v for k, v in old_overrides.items() if k != name}
        if len(servers) == len(old_servers) and name not in old_overrides:
            return
        mcp["servers"] = servers
        mcp["overrides"] = overrides
        update_project_config(self._project_config_path, mcp=mcp)

    def write_project_override(self, name: str, override: McpServerOverride) -> None:
        """字段级合并写项目层 override(设计 §10)。

        请求中未设置的字段保留项目层原值(如 Web server toggle 只发 {enabled}
        不丢工具选择);显式 null 清除该字段回落到用户层/server 默认语义;
        显式 [] 仍是空选择;四个字段相互独立。全清后 override 条目整体删除。
        """
        data = read_project_config(self._project_config_path)
        mcp = dict(data.get("mcp") or {})
        overrides = dict(mcp.get("overrides") or {})
        merged = {**dict(overrides.get(name) or {}),
                  **override.model_dump(exclude_unset=True)}
        merged = {k: v for k, v in merged.items() if v is not None}
        if merged:
            overrides[name] = merged
        else:
            overrides.pop(name, None)
        mcp["overrides"] = overrides
        update_project_config(self._project_config_path, mcp=mcp)


def _file_version(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _read_mcp_section(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not text:
        return {}
    data = json.loads(text)
    return data.get("mcp") or {}
