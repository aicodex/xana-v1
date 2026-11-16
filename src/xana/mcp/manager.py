"""MCP 连接与发布状态机(docs/superpowers/specs/2026-11-13-mcp-design.md §6、§8、§9)。

manager 是连接与发布状态的唯一权威。初始连接、配置热加载、手动 reload、退避重连与
tools/list_changed 全部走同一 discover → audit → publish 入口;tools/list 全分页耗尽
后才审核发布,任一页失败或超过 256 上限都不发布部分结果。健康检查失败、工具调用
传输失败与连接意外关闭共用失效入口:摘除本 generation 全部工具,按 2→60 秒指数
退避重连;恢复尊重用户禁用选择。config/protocol 错误进入 failed,不做无意义重试。
stale generation 与 close 后的回调一律不发布。运行状态以 manager 为权威,
audit.jsonl 为只追加审计证据。
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp.shared.exceptions import McpError

from xana.config.system_model import McpServer, McpServerOverride
from xana.mcp.audit import AuditFinding, audit_tool, tool_fingerprint
from xana.mcp.audit_store import AuditStore
from xana.mcp.client import McpClientConnection, McpTransportError
from xana.mcp.config import McpConfigStore
from xana.mcp.contracts import (
    ConfiguredServer,
    McpConfigSnapshot,
    ServerView,
    ToolView,
)
from xana.mcp.naming import qualified_tool_name
from xana.mcp.tools import McpTool, build_tool_spec
from xana.multimodal.files import FileMediaStore
from xana.tools.interfaces import ToolRegistry

MAX_TOOLS_PER_SERVER = 256
_PING_TIMEOUT_S = 10.0


@dataclass
class _ToolOutcome:
    """一次发现中单个工具的审核/隔离/选择结果,供视图投影与后续选择重算。"""

    raw_name: str
    fingerprint: str
    findings: tuple[AuditFinding, ...]
    qualified: str | None     # None = 被隔离或未通过审核


@dataclass
class _ServerState:
    name: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    configured: ConfiguredServer | None = None
    generation: int = 0
    status: str = "connecting"
    connection: Any = None
    published: dict[str, str] = field(default_factory=dict)   # 限定名 → 定义指纹
    outcomes: tuple[_ToolOutcome, ...] = ()
    retry_task: asyncio.Task | None = None
    refresh_task: asyncio.Task | None = None
    attempts: int = 0
    error: str | None = None
    retry_in_s: float | None = None


def _fingerprint(raw: Mapping[str, Any]) -> str:
    try:
        return tool_fingerprint(raw)
    except Exception:
        return ""


def _conn_key(cfg: ConfiguredServer) -> tuple[Any, ...]:
    """连接定义比较键;快照内 env/headers 是 mappingproxy,不走 model_dump。"""
    server = cfg.server
    return (
        server.name, server.transport, server.command, tuple(server.args),
        tuple(sorted(server.env.items())), server.cwd, server.url,
        tuple(sorted(server.headers.items())), server.startup_timeout_s,
        server.tool_timeout_s, cfg.config_error, cfg.enabled,
    )


def _sel_key(cfg: ConfiguredServer) -> tuple[Any, ...]:
    return (cfg.enabled_tools, cfg.disabled_tools, cfg.deferred)


class McpManager:
    """配置快照、连接 generation、逐 server 锁、重试任务与已审核工具快照的唯一权威。"""

    def __init__(
        self,
        store: McpConfigStore,
        registry: ToolRegistry,
        media_store: FileMediaStore,
        *,
        connector: Callable[..., Any] = McpClientConnection.connect,
        auditor: Callable[[Mapping[str, Any]], tuple[AuditFinding, ...]] = audit_tool,
        environ: Mapping[str, str] | None = None,
        sleeper: Callable[[float], Any] = asyncio.sleep,
        health_interval_s: float = 30.0,
        audit_store: AuditStore | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._media = media_store
        self._connector = connector
        self._auditor = auditor
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._sleeper = sleeper
        self._health_interval_s = health_interval_s
        self._audit = audit_store or AuditStore(store.audit_store_path)
        self._states: dict[str, _ServerState] = {}
        self._snapshot: McpConfigSnapshot | None = None
        self._watch_versions: tuple[tuple[int, int], tuple[int, int]] = ((0, 0), (0, 0))
        self._load_error: str | None = None
        self._closed = True
        self._generation = 0
        self._watch_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if not self._closed:
            raise RuntimeError("McpManager 已启动")
        self._closed = False
        self._snapshot = self._load_snapshot()
        self._watch_versions = self._store.versions()
        for configured in self._snapshot.servers:
            self._ensure_state(configured.server.name).configured = configured
        await asyncio.gather(*(self._refresh(st) for st in self._states.values()))
        self._watch_task = asyncio.create_task(self._watch_loop())
        self._health_task = asyncio.create_task(self._health_loop())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._watch_task, self._health_task):
            if task is not None:
                task.cancel()
        for st in self._states.values():
            self._cancel_retry(st)
            if st.refresh_task is not None and not st.refresh_task.done():
                st.refresh_task.cancel()
        for st in self._states.values():
            async with st.lock:
                await self._teardown_locked(st)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("McpManager 未启动或已关闭")

    def _load_snapshot(self) -> McpConfigSnapshot:
        try:
            snapshot = self._store.load()
        except Exception as exc:
            # 只记异常类型:ValidationError 等原文可能回显配置值(含秘密)
            self._load_error = f"配置加载失败({type(exc).__name__})"
            return self._snapshot or McpConfigSnapshot(servers=())
        self._load_error = None
        return snapshot

    @property
    def load_error(self) -> str | None:
        """最近一次配置加载/热加载失败的脱敏说明;异常只记类型名,不回显原文。"""
        return self._load_error

    def _ensure_state(self, name: str) -> _ServerState:
        st = self._states.get(name)
        if st is None:
            st = _ServerState(name=name)
            self._states[name] = st
        return st

    def _next_generation(self) -> int:
        self._generation += 1
        return self._generation

    # ---------- 管理 API(Task 5 投影层调用)----------

    async def reload(self, name: str | None = None) -> tuple[ServerView, ...]:
        """name 为 None 重读配置并对变化项执行状态机;否则强制重连该 server(重置退避)。"""
        self._require_open()
        self._snapshot = self._load_snapshot()
        if name is None:
            await self._apply_snapshot()
        else:
            configured = next((c for c in self._snapshot.servers
                               if c.server.name == name), None)
            if configured is None:
                raise KeyError(name)
            st = self._ensure_state(name)
            st.configured = configured
            st.attempts = 0    # 手动 reload 重置退避
            await self._refresh(st)
        return self.list()

    async def register_server(self, config: McpServer) -> ServerView:
        """注册/替换项目级 server(调用方传入新构造的配置对象,非快照冻结实例)。"""
        self._require_open()
        if not isinstance(config, McpServer):
            raise TypeError("config 必须是 McpServer")
        self._store.write_project_server(config)
        await self.reload(config.name)
        return self._view_of(config.name)

    async def remove_server(self, name: str) -> None:
        """删除项目 server 定义与 override;同名用户级定义随后恢复生效。"""
        self._require_open()
        self._store.delete_project_server(name)
        await self.reload()

    async def set_availability(self, name: str,
                               override: McpServerOverride) -> ServerView:
        """写项目 override 并立即对该 server 执行状态机;故障禁用绝不回写用户配置。"""
        self._require_open()
        if name not in self._states:
            raise KeyError(name)
        self._store.write_project_override(name, override)
        await self.reload(name)
        return self._view_of(name)

    def list(self) -> tuple[ServerView, ...]:
        views = []
        for st in self._states.values():
            if st.configured is not None:
                views.append(self._build_view(st, st.configured))
        return tuple(views)

    def _view_of(self, name: str) -> ServerView:
        for view in self.list():
            if view.name == name:
                return view
        raise KeyError(name)

    # ---------- 配置热加载 ----------

    async def _apply_snapshot(self) -> None:
        snapshot = self._snapshot or McpConfigSnapshot(servers=())
        kept: set[str] = set()
        for configured in snapshot.servers:
            kept.add(configured.server.name)
            st = self._ensure_state(configured.server.name)
            prev = st.configured
            st.configured = configured
            if (prev is not None and _conn_key(prev) == _conn_key(configured)
                    and _sel_key(prev) == _sel_key(configured)):
                continue    # 连接定义与选择均未变化,不打扰现有连接
            st.attempts = 0
            await self._refresh(st)
        for name in [n for n in self._states if n not in kept]:
            st = self._states.pop(name)
            self._cancel_retry(st)
            async with st.lock:
                await self._teardown_locked(st)

    async def _watch_loop(self) -> None:
        interval = (self._snapshot.watch_interval_s if self._snapshot else 2.0)
        try:
            last = self._watch_versions
            while not self._closed:
                await self._sleeper(interval)
                if self._closed:
                    return
                try:
                    current = self._store.versions()
                except OSError:
                    continue
                if current == last:
                    continue
                last = current
                try:
                    await self.reload()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # 意外错误不得杀死 watch:记录脱敏错误(不回显异常原文,
                    # 可能含配置秘密),保留旧快照与在册工具,继续轮询
                    self._load_error = f"配置热加载失败({type(exc).__name__})"
        except asyncio.CancelledError:
            return

    async def _health_loop(self) -> None:
        try:
            while not self._closed:
                await self._sleeper(self._health_interval_s)
                if self._closed:
                    return
                for st in list(self._states.values()):
                    if st.status != "connected" or st.connection is None:
                        continue
                    async with st.lock:
                        if (self._closed or st.status != "connected"
                                or st.connection is None):
                            continue
                        conn = st.connection
                        try:
                            await asyncio.wait_for(conn.ping(), timeout=_PING_TIMEOUT_S)
                        except Exception:
                            if st.connection is conn:
                                await self._invalidate_locked(st, "健康检查 ping 失败")
        except asyncio.CancelledError:
            return

    # ---------- 连接与发布 ----------

    async def _refresh(self, st: _ServerState) -> None:
        """全新 generation:连接(含初始发现)→ 审核 → 原子发布。

        连接成功前不摘除旧 generation:热加载/手动 reload 失败时保留上一份
        已审核工具与旧连接;传输失效路径在进入 _refresh 前已完成摘除。
        """
        async with st.lock:
            if self._closed or st.configured is None:
                return
            cfg = st.configured
            self._cancel_retry(st)
            if not cfg.enabled:
                await self._teardown_locked(st)
                st.status, st.error, st.retry_in_s = "disabled", None, None
                return
            if cfg.config_error is not None:
                await self._teardown_locked(st)
                st.status, st.error, st.retry_in_s = "failed", cfg.config_error, None
                return
            st.status, st.error, st.retry_in_s = "connecting", None, None
            gen = self._next_generation()
            try:
                conn, raw_tools = await self._connector(
                    cfg, environ=self._environ,
                    on_tools_changed=lambda: self._on_tools_changed(st.name, gen),
                    on_closed=lambda exc=None: self._on_connection_closed(st.name, gen, exc),
                )
            except McpTransportError as exc:
                # 传输失败按失效处理:摘除旧 generation 并进入退避重试(新配置)
                await self._invalidate_locked(st, f"传输错误:{exc}")
                return
            except McpError as exc:
                await self._teardown_locked(st)
                st.status = "failed"
                st.error = f"协议错误:{exc.error.message}"
                return
            except Exception:
                if st.connection is not None:
                    st.status = "connected"    # 意外错误:旧 generation 完整保留
                raise
            st.generation = gen
            await self._teardown_locked(st)
            st.connection = conn
            await self._publish_locked(st, conn, raw_tools, gen)

    async def _rediscover(self, st: _ServerState, gen: int) -> None:
        """同连接重新 discover → audit → publish(tools/list_changed 路径)。"""
        async with st.lock:
            if (self._closed or st.generation != gen
                    or st.connection is None or st.status != "connected"):
                return
            try:
                raw_tools = await st.connection.list_tools()
            except McpTransportError as exc:
                await self._invalidate_locked(st, f"传输错误:{exc}")
                return
            await self._publish_locked(st, st.connection, raw_tools, gen)

    async def _publish_locked(self, st: _ServerState, conn: Any,
                              raw_tools: tuple[Mapping[str, Any], ...], gen: int) -> None:
        """完整分页结果先整体审核,再在同一事件循环临界区做注销/注册差异。"""
        if self._closed or st.generation != gen:
            return
        if len(raw_tools) > MAX_TOOLS_PER_SERVER:
            await conn.close()
            if st.connection is conn:
                st.connection = None
            st.status = "failed"
            st.error = (f"tools/list 返回 {len(raw_tools)} 个工具,"
                        f"超过上限 {MAX_TOOLS_PER_SERVER},已整体隔离")
            return
        cfg = st.configured
        assert cfg is not None
        outcomes: list[_ToolOutcome] = []
        candidates: list[tuple[str, str, Mapping[str, Any], str]] = []
        seen_raw: set[str] = set()
        seen_qualified: set[str] = set()
        for raw in raw_tools:
            raw_name = str(raw.get("name") or "")
            findings = tuple(self._auditor(raw))          # 审核先于指纹
            fp = _fingerprint(raw)
            self._audit.record(st.name, raw_name, fp, gen, findings)
            if findings:
                outcomes.append(_ToolOutcome(raw_name, fp, findings, None))
                continue
            qualified = qualified_tool_name(st.name, raw_name)
            conflict = None
            if raw_name in seen_raw:
                conflict = "重复原始工具名,已隔离"
            elif qualified in seen_qualified:
                conflict = "清洗后名称冲突,已隔离"
            seen_raw.add(raw_name)
            seen_qualified.add(qualified)
            if conflict is not None:
                finding = AuditFinding("naming.conflict", "$.name", conflict)
                self._audit.record(st.name, raw_name, fp, gen, (finding,))
                outcomes.append(_ToolOutcome(raw_name, fp, (finding,), None))
                continue
            outcome = _ToolOutcome(raw_name, fp, (), qualified)
            outcomes.append(outcome)
            if self._selected(cfg, raw_name):            # 审核先于用户选择计算
                candidates.append((qualified, raw_name, raw, fp))
        st.outcomes = tuple(outcomes)
        desired = {q: (raw_name, raw, fp) for q, raw_name, raw, fp in candidates}
        # 同一临界区:先注销消失/变更的,再按限定名字序注册新增/变更
        for qualified in sorted(set(st.published) - set(desired)):
            await self._registry.unregister(qualified)
            del st.published[qualified]
        for qualified in sorted(desired):
            raw_name, raw, fp = desired[qualified]
            if st.published.get(qualified) == fp:
                continue                              # 相同定义不替换对象
            if qualified in st.published:
                await self._registry.unregister(qualified)   # 注销自己的旧对象
            if await self._registry.get(qualified) is not None:
                finding = AuditFinding("naming.conflict", "$.name",
                                       "与现有工具冲突,已隔离")
                self._audit.record(st.name, raw_name, fp, gen, (finding,))
                st.outcomes = tuple(
                    _ToolOutcome(o.raw_name, o.fingerprint, (finding,), None)
                    if o.raw_name == raw_name and not o.findings else o
                    for o in st.outcomes)
                continue
            tool = McpTool(
                build_tool_spec(cfg, raw, qualified),
                conn,
                lambda n=st.name, g=gen: self._on_tool_transport_failure(n, g),
                lambda g=gen, c=conn: (not self._closed and st.generation == g
                                       and st.connection is c),
                self._media,
            )
            await self._registry.register(tool)
            st.published[qualified] = fp
        st.status = "connected"
        st.attempts = 0
        st.error = None
        st.retry_in_s = None

    @staticmethod
    def _selected(cfg: ConfiguredServer, raw_name: str) -> bool:
        if cfg.enabled_tools is not None and raw_name not in cfg.enabled_tools:
            return False
        return raw_name not in cfg.disabled_tools

    # ---------- 失效、退避与重试 ----------

    async def _invalidate_locked(self, st: _ServerState, error: str) -> None:
        """传输失效共用入口(调用方持有 st.lock):摘除本 generation 全部工具。"""
        st.generation = self._next_generation()
        await self._teardown_locked(st)
        cfg = st.configured
        if cfg is None or not cfg.enabled:
            st.status, st.error, st.retry_in_s = "disabled", None, None
            return
        if cfg.config_error is not None:
            st.status = "failed"
            st.error, st.retry_in_s = cfg.config_error, None
            return
        snapshot = self._snapshot or McpConfigSnapshot(servers=())
        delay = min(snapshot.reconnect_max_s,
                    snapshot.reconnect_initial_s * (2 ** st.attempts))
        st.attempts += 1
        st.status = "retry_waiting"
        st.error, st.retry_in_s = error, delay
        st.retry_task = asyncio.create_task(self._retry_after(st, delay, st.generation))

    async def _retry_after(self, st: _ServerState, delay: float, gen: int) -> None:
        try:
            await self._sleeper(delay)
        except asyncio.CancelledError:
            return
        if self._closed or st.generation != gen:
            return
        try:
            await self._refresh(st)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 重连中的意外(非传输)错误:进入 failed 且不无限重试,不得卡死
            # retry_waiting;错误只记类型名,不回显异常原文(可能含配置秘密)
            async with st.lock:
                if self._closed or st.status not in ("connecting", "retry_waiting"):
                    return    # 已有更新的操作接管
                st.generation = self._next_generation()
                await self._teardown_locked(st)
                st.status = "failed"
                st.error = f"重连意外错误({type(exc).__name__})"
                st.retry_in_s = None

    def _cancel_retry(self, st: _ServerState) -> None:
        task = st.retry_task
        st.retry_task = None
        st.retry_in_s = None
        if (task is not None and not task.done()
                and task is not asyncio.current_task()):
            task.cancel()

    async def _teardown_locked(self, st: _ServerState) -> None:
        for qualified in sorted(st.published):
            await self._registry.unregister(qualified)
        st.published.clear()
        conn, st.connection = st.connection, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass

    def _on_tool_transport_failure(self, name: str, gen: int) -> None:
        st = self._states.get(name)
        if st is None or self._closed or st.generation != gen:
            return
        st.generation = self._next_generation()    # 旧工具立即失效,不再可用
        bumped = st.generation
        asyncio.create_task(self._invalidate_after_bump(st, bumped, "工具调用传输失败"))

    def _on_connection_closed(self, name: str, gen: int,
                              exc: BaseException | None) -> None:
        if self._closed:
            return
        st = self._states.get(name)
        if st is None or st.generation != gen or st.connection is None:
            return
        st.generation = self._next_generation()
        bumped = st.generation
        asyncio.create_task(self._invalidate_after_bump(st, bumped, "连接意外关闭"))

    async def _invalidate_after_bump(self, st: _ServerState, bumped: int,
                                     error: str) -> None:
        async with st.lock:
            if self._closed or st.generation != bumped:
                return    # 已有更新的 generation 接管
            await self._invalidate_locked(st, error)

    def _on_tools_changed(self, name: str, gen: int) -> None:
        if self._closed:
            return
        st = self._states.get(name)
        if st is None or st.generation != gen or st.connection is None:
            return
        if st.refresh_task is not None and not st.refresh_task.done():
            return                       # 逐 server 防抖:合并密集 list_changed
        st.refresh_task = asyncio.create_task(self._rediscover(st, gen))

    # ---------- 视图 ----------

    def _build_view(self, st: _ServerState, cfg: ConfiguredServer) -> ServerView:
        tools = []
        for o in st.outcomes:
            qualified = o.qualified or qualified_tool_name(st.name, o.raw_name)
            if o.findings:
                status = "quarantined"
            elif st.status != "connected" or o.qualified is None:
                status = "unavailable" if st.status != "connected" else "disabled_user"
            elif o.qualified not in st.published:
                status = "disabled_user"
            else:
                status = "enabled"
            tools.append(ToolView(name=qualified, raw_name=o.raw_name, status=status,
                                  fingerprint=o.fingerprint, findings=o.findings))
        return ServerView(
            name=st.name, origin=cfg.origin, transport=cfg.server.transport,
            enabled=cfg.enabled, status=st.status,                   # type: ignore[arg-type]
            config_error=cfg.config_error, error=st.error,
            retry_in_s=st.retry_in_s if st.status == "retry_waiting" else None,
            tools=tuple(tools),
        )


__all__ = ["MAX_TOOLS_PER_SERVER", "McpManager"]
