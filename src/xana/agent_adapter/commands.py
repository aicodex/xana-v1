"""DefaultCommandRouter: routes 11 built-in slash commands.

Commands that mutate persistent state delegate to SessionConfigStore;
commands that mutate ephemeral state (history clear/compact, cancel current
turn) talk to ActiveContextStore and AgentGateway directly. /help is
self-contained — only the router knows the full command list.

Adapters must:
  1. call route_command() on every inbound;
  2. if CommandResult.ok is False AND payload['pass_through'] is True, treat
     the inbound as plain user text and forward to the agent;
  3. otherwise consume CommandResult.message + payload.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from xana.config.paths import project_home, project_id, project_root
from xana.agent_adapter.contracts import CommandResult, CommandSpec, InboundMessage
from xana.agent_adapter.interfaces import CommandRouter
from xana.memory.interfaces import HistoryMirror
from xana.harness_sdk.turn_control import TurnCoordinator
from xana.harness_sdk.contracts import AbortSignal
from xana.harness_sdk.interfaces import Harness
from xana.agents.interfaces import AgentRequestMapper
from xana.agents.contracts import AgentRequest
from xana.config.contracts import ModelConfig
from xana.config.loader import resolve_model_config
from xana.config.system_model import ModelEntry

if TYPE_CHECKING:
    from xana.session_config.contracts import SessionConfig


_HandlerFn = Callable[["DefaultCommandRouter", InboundMessage, str], Awaitable[CommandResult]]


_logger = logging.getLogger(__name__)


_VIEW_MODES = ("detailed", "grouped", "summary")


def _option_buttons(command: str, options: tuple[str, ...], per_row: int = 3) -> list[list[dict]]:
    """选项列表 → send 型按钮行(点击即发送 `command option`),按 per_row 分行。"""
    btns = [{"text": o, "send": f"{command} {o}"} for o in options]
    return [btns[i:i + per_row] for i in range(0, len(btns), per_row)]


class DefaultCommandRouter(CommandRouter):
    def __init__(
        self,
        *,
        session_config_store: Any = None,  # SessionConfigStore | None — typed loose to avoid cycles in tests
        active_context_store: Any = None,
        history_mirror: HistoryMirror | None = None,
        gateway: Any = None,
        control: TurnCoordinator | None = None,
        harness: Harness | None = None,
        request_mapper: AgentRequestMapper | None = None,
        available_models: tuple[str, ...] = (),
        available_search_providers: tuple[str, ...] = (),
        available_efforts: tuple[str, ...] = ("high", "max"),
        model_registry: Any = None,
        provider: str = "",
        model: str = "",
        model_configs: dict[str, ModelConfig] | None = None,
        progress_sink: Any = None,
        default_agent: str = "chat",
        agent_registry: Any = None,   # AgentRegistry | None — /agent 前刷新(热加载)
    ) -> None:
        self.session_config_store = session_config_store
        self.default_agent = default_agent
        self.model_configs = dict(model_configs) if model_configs is not None else None
        self.agent_registry = agent_registry
        self.active_context_store = active_context_store
        self.history_mirror = history_mirror
        self.gateway = gateway
        self.control = control
        self.harness = harness
        self.request_mapper = request_mapper
        self.available_models = available_models
        self.available_search_providers = available_search_providers
        self.available_efforts = available_efforts
        self.model_registry = model_registry
        self.provider = provider
        self.model = model
        self.progress_sink = progress_sink
        self._compact_cancels: dict[str, tuple[str, AbortSignal]] = {}

        self._specs: tuple[CommandSpec, ...] = (
            CommandSpec(name="/help",     description="显示命令菜单。用法 `/help` 或 `/help /命令`"),
            CommandSpec(name="/h",        description="同 /help"),
            CommandSpec(name="/clear",    description="清空当前会话上下文"),
            CommandSpec(name="/agent",    description="用指定 agent 开新会话,无参列出可选;allow-exec / deny-exec <名字> 开关其 Bash"),
            CommandSpec(name="/compact",  description="用 LLM 把本会话历史压缩成摘要"),
            CommandSpec(name="/model",    description="切换本会话模型，无参列出可选"),
            CommandSpec(name="/effort",   description="推理强度切换，无参列出可选"),
            CommandSpec(name="/streaming", description="token 流开关 on/off(无参数切换)"),
            CommandSpec(name="/thinking", description="思考链开关 on/off(无参数切换)"),


            CommandSpec(name="/cancel",   description="取消当前 turn"),
            CommandSpec(name="/project",  description="显示当前项目"),
            CommandSpec(name="/search_provider", description="搜索引擎切换"),
            CommandSpec(name="/view",    description="工具过程显示模式:详细 / 分组 / 摘要(无参列出可选)"),
            CommandSpec(name="/resume",  description="列出可恢复的已停止 run,或 /resume <run_id> 续跑"),
            CommandSpec(name="/tasks",   description="任务面板:列出执行中/已停止/已完成的 run(可 /tasks <session_id> 过滤)"),
        )
        self._handlers: dict[str, _HandlerFn] = {
            "/help":            DefaultCommandRouter._cmd_help,
            "/h":               DefaultCommandRouter._cmd_help,
            "/clear":           DefaultCommandRouter._cmd_clear,
            "/agent":           DefaultCommandRouter._cmd_agent,
            "/compact":         DefaultCommandRouter._cmd_compact,
            "/model":           DefaultCommandRouter._cmd_model,
            "/effort":          DefaultCommandRouter._cmd_effort,
            "/streaming":       DefaultCommandRouter._cmd_streaming,
            "/thinking":        DefaultCommandRouter._cmd_thinking,

            "/search_provider": DefaultCommandRouter._cmd_search_provider,
            "/view":            DefaultCommandRouter._cmd_view,
            "/cancel":          DefaultCommandRouter._cmd_cancel,

            "/project":         DefaultCommandRouter._cmd_project,
            "/resume":          DefaultCommandRouter._cmd_resume,
            "/tasks":           DefaultCommandRouter._cmd_tasks,
            # 隐藏命令:卡片取消按钮触发,不进 _specs(/help 不显示)。
            "/compact_cancel":  DefaultCommandRouter._cmd_compact_cancel,
        }

    async def list_commands(self) -> tuple[CommandSpec, ...]:
        return self._specs

    async def route_command(self, inbound: InboundMessage) -> CommandResult:
        text = inbound.text.strip()
        if not text.startswith("/"):
            return CommandResult(ok=False, message="", payload={"pass_through": True})
        parts = text.split(maxsplit=1)
        name = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        handler = self._handlers.get(name)
        if handler is None:
            return CommandResult(
                ok=False, message=f"未注册的命令 {name}",
                payload={"pass_through": False, "unknown_command": True},
            )
        return await handler(self, inbound, args)

    async def _cmd_help(self, inbound: InboundMessage, args: str) -> CommandResult:
        args = args.strip()
        if args:
            target = args.split()[0]
            for spec in self._specs:
                if spec.name == target:
                    return CommandResult(ok=True, message=f"{spec.name} — {spec.description}")
            return CommandResult(ok=False, message=f"未知命令 {target}")
        lines = ["可用命令:"]
        for spec in self._specs:
            lines.append(f"  {spec.name:<20} {spec.description}")
        # 命令名在 TG 正文里本身可点击发送 → 设 linkify_commands,TG 隐去按钮。
        # 飞书 markdown 无内联点击发送能力,只能用按钮(send 型,点击即发命令)。
        sends = [{"text": spec.name, "send": spec.name} for spec in self._specs]
        rows = [sends[i:i + 2] for i in range(0, len(sends), 2)]
        return CommandResult(ok=True, message="\n".join(lines),
                             payload={"buttons": rows, "linkify_commands": True})

    def _rotate_session(self, inbound: InboundMessage) -> str | None:
        """为当前外部会话 (adapter_id, thread_id) 换一个新 session id;失败返回 None。

        I17 (docs/design/session-and-card-state.md §1.7): rotation does NOT wipe
        the old session's active_context, memory tree, or bg runs — they stay
        under the old sid. The next inbound on the same external thread resolves
        to the NEW sid via SessionResolver."""
        adapters = getattr(self.gateway, "adapters", {}) if self.gateway is not None else {}
        adapter = adapters.get(inbound.adapter_id) if adapters else None
        # thread_id: feishu/tg = chat_id; cli = "" (single host = single thread)
        if inbound.metadata and "chat_id" in inbound.metadata:
            external_thread_id = inbound.metadata.get("chat_id") or ""
        else:
            external_thread_id = ""
        rotate = getattr(adapter, "rotate_session", None) if adapter is not None else None
        if rotate is None:
            return None
        try:
            new_sid = rotate(external_thread_id)
        except Exception:
            return None
        return None if new_sid == inbound.session_id else new_sid

    async def _cmd_clear(self, inbound: InboundMessage, args: str) -> CommandResult:
        new_sid = self._rotate_session(inbound)
        if new_sid is None:
            return CommandResult(
                ok=False,
                message="[clear] 未能换会话 id (adapter 不支持 rotate_session)",
            )
        # 清空上下文不换 agent:把会话绑定的 agent 带到新会话
        cfg = await self._current_config(inbound.session_id)
        if cfg is not None and cfg.agent:
            await self._persist_with(new_sid, agent=cfg.agent)
        return CommandResult(
            ok=True, message=f"[cleared] 已切换会话 id → {new_sid}",
            payload={"new_session_id": new_sid},
        )

    async def _cmd_agent(self, inbound: InboundMessage, args: str) -> CommandResult:
        """/agent:列出可用 agent;/agent <名字>:换新会话 id 并绑定该 agent(会话创建时绑定、之后不切换,
        docs/design/agent-delegation-design.md §3.1)。旧会话原样保留。"""
        if self.agent_registry is not None:
            await self.agent_registry.refresh(force=True)   # 用户主动查看:立即扫描,不受节流影响
        agents = dict(getattr(self.gateway, "agents", {}) or {}) if self.gateway is not None else {}
        name = args.strip()
        op, _, rest = name.partition(" ")
        if op in ("allow-exec", "deny-exec"):
            return await self._agent_exec(op == "allow-exec", rest)
        if not name:
            cfg = await self._current_config(inbound.session_id)
            current = (cfg.agent if cfg else None) or self.default_agent
            lines = [f"当前 agent: {current}", "可用 agent:"]
            for n in sorted(agents):
                lines.append(f"  {n:<16} {agents[n].spec().component.description}")
            return CommandResult(ok=True, message="\n".join(lines),
                                 payload={"buttons": _option_buttons("/agent", tuple(sorted(agents)))})
        if name not in agents:
            return CommandResult(ok=False, message=f"未知 agent {name}(/agent 查看可选)")
        new_sid = self._rotate_session(inbound)
        if new_sid is None:
            return CommandResult(ok=False, message="[agent] 未能开新会话 (adapter 不支持 rotate_session)")
        await self._persist_with(new_sid, agent=None if name == self.default_agent else name)
        return CommandResult(
            ok=True, message=f"[agent] 已开新会话 {new_sid},由 {name} 负责",
            payload={"new_session_id": new_sid},
        )

    async def _agent_exec(self, allowed: bool, rest: str) -> CommandResult:
        """/agent allow-exec <名字> <原因> | /agent deny-exec <名字>:用户开关文件 agent 的 Bash
        (iteration-agent-design §5.4;模型改不了这一项)。"""
        name, _, reason = rest.strip().partition(" ")
        if not name or (allowed and not reason.strip()):
            return CommandResult(ok=False, message="用法:/agent allow-exec <名字> <原因> 或 /agent deny-exec <名字>")
        if self.agent_registry is None:
            return CommandResult(ok=False, message="agent_registry 未配置")
        try:
            path = await self.agent_registry.set_allow_exec(name, allowed, reason)
        except (LookupError, ValueError) as exc:
            return CommandResult(ok=False, message=str(exc))
        if allowed:
            return CommandResult(ok=True, message=(
                f"[agent] 已给 {name} 开启 Bash(原因:{reason.strip()}),写入 {path}。它在 tools 里选了 exec 组才会拿到 Bash;"
                "评测时它的结果会标注可信度降低。"))
        return CommandResult(ok=True, message=f"[agent] 已关闭 {name} 的 Bash,写入 {path}")

    async def _cmd_compact(self, inbound: InboundMessage, args: str) -> CommandResult:
        from xana.harness_sdk.contracts import AbortSignal
        if self.active_context_store is None or self.history_mirror is None or self.model_registry is None:
            return CommandResult(False, "active_context_store / history_mirror / model_registry 未配置")
        if self.control is None:
            return CommandResult(False, "会话执行协调器未配置")
        run_id = inbound.metadata.get("command_run_id") or f"compact-{uuid.uuid4().hex}"
        abort = AbortSignal()
        self._compact_cancels[run_id] = (inbound.session_id, abort)
        async def perform(owner_abort):
            async def follow():
                await owner_abort.wait()
                abort.trigger(owner_abort.reason or "cancelled")
            linked = asyncio.create_task(follow())
            try:
                return await self._compact_session(inbound, run_id, abort)
            finally:
                linked.cancel()
                try:
                    await linked
                except asyncio.CancelledError:
                    pass
        try:
            return await self.control.maintenance(inbound.session_id, run_id, perform)
        finally:
            self._compact_cancels.pop(run_id, None)

    async def _compact_session(self, inbound, run_id, abort) -> CommandResult:
        from xana.harness_sdk.compact import summary_progress, _CompactCancelled
        from xana.harness_sdk.contracts import ProgressEvent
        from xana.context.message import CompactSummary
        if self.harness is None or self.request_mapper is None:
            return CommandResult(False, "压缩执行服务未配置")
        agent = await self._session_agent(inbound.session_id)
        active = self.control.active(inbound.session_id)
        if active and active[0].context is not None:
            context = dataclasses.replace(active[0].context, abort=abort)
        else:
            target = self.gateway.agents.get(agent) if self.gateway is not None else None
            if target is None:
                return CommandResult(False, f"未知 agent {agent}")
            context = await self.request_mapper.to_harness_context(target.spec(), AgentRequest(
                inbound.session_id, "", inbound.adapter_id, metadata={"gateway_run_id": run_id}))
            context = dataclasses.replace(context, abort=abort)
        model = context.model
        messages = await self.active_context_store.snapshot(inbound.session_id, agent)
        async def report(kind, payload):
            if self.progress_sink is not None:
                await self.progress_sink.report(ProgressEvent(inbound.session_id, run_id, kind, {
                    "kind": kind, "session_id": inbound.session_id, "run_id": run_id,
                    "input_id": inbound.metadata.get("input_id"), **payload}))
        async def emit(text="", phase="running"):
            if abort.aborted:
                raise _CompactCancelled()
            bar, status = summary_progress(text)
            await report("compact_progress", {"model": model, "phase": phase, "text": bar,
                "sections_done": bar.count("▰"), "partial_summary": text})
        summary = ""
        cancelled = False
        failed = False
        try:
            await report("compact_started", {})
            if not messages or (len(messages) == 1 and messages[0].role == "compact"):
                summary = "历史已压缩，无新增可压缩内容"
            else:
                await emit()
                result = await self.harness.compact(context, on_progress=emit)
                if abort.aborted:
                    raise _CompactCancelled()
                if result is None:
                    raise ValueError("模型未返回有效且更短的摘要，原始历史已保留")
                summary = CompactSummary.render(result.summary, result.transcript_path)
        except _CompactCancelled:
            cancelled = True
        except Exception:
            _logger.exception("compaction failed")
            failed = True
        await report("compact_finalized", {"summary": summary, "cancelled": cancelled, "error": failed,
            "compacted_n": len(messages) if not (cancelled or failed) else 0})
        return CommandResult(not (cancelled or failed),
            ("" if self.progress_sink is not None and not (cancelled or failed) else summary or
             ("压缩已取消" if cancelled else "压缩失败")),
            {"run_id": run_id, "silent": self.progress_sink is not None})

    async def _cmd_compact_cancel(self, inbound: InboundMessage, args: str) -> CommandResult:
        entry = self._compact_cancels.get(args.strip())
        if entry is not None and entry[0] == inbound.session_id:
            entry[1].trigger("user_cancel")
        return CommandResult(True, "已请求取消压缩", {"silent": True})

    async def _persist_with(self, session_id: str, **changes) -> "SessionConfig":
        """只改 changes 里的字段,其余(含 prompt_profile / metadata)原样保留。"""
        if self.session_config_store is None:
            raise RuntimeError("session_config_store 未配置")
        current = await self.session_config_store.get(session_id)
        new = dataclasses.replace(current, **changes)
        await self.session_config_store.save(new)
        return new

    async def _session_agent(self, session_id: str) -> str:
        """会话当前绑定的 agent(/agent 设置),没有则默认 agent;上下文按 (会话, agent) 存放。"""
        cfg = await self._current_config(session_id)
        return (cfg.agent if cfg is not None and cfg.agent else None) or self.default_agent

    async def _current_config(self, session_id: str) -> "SessionConfig | None":
        if self.session_config_store is None:
            return None
        return await self.session_config_store.get(session_id)

    async def _cmd_model(self, inbound: InboundMessage, args: str) -> CommandResult:
        target = args.strip()
        if not target:
            if not self.available_models:
                return CommandResult(ok=True, message="未配置可选模型")
            cfg = await self._current_config(inbound.session_id)
            cur = (cfg.model if cfg and cfg.model else None) or self.model or "(默认)"
            return CommandResult(
                ok=True, message=f"当前模型: {cur}",
                payload={"buttons": _option_buttons("/model", self.available_models)})
        if self.available_models and target not in self.available_models:
            return CommandResult(ok=False, message=f"未知模型 {target}; 可选: " + ", ".join(self.available_models))
        new = await self._persist_with(inbound.session_id, model=target)
        return CommandResult(ok=True, message=f"模型切换为 {new.model}")

    async def _cmd_effort(self, inbound: InboundMessage, args: str) -> CommandResult:
        target = args.strip().lower()
        opts = ", ".join(self.available_efforts)
        if not target:
            cfg = await self._current_config(inbound.session_id)
            cur = (cfg.effort if cfg and cfg.effort else None) or "(默认)"
            return CommandResult(
                ok=True, message=f"当前 effort: {cur}",
                payload={"buttons": _option_buttons("/effort", self.available_efforts)})
        if target not in self.available_efforts:
            return CommandResult(ok=False, message=f"effort 必须为 {opts}")
        new = await self._persist_with(inbound.session_id, effort=target)
        return CommandResult(ok=True, message=f"effort 切换为 {new.effort}")

    async def _cmd_streaming(self, inbound: InboundMessage, args: str) -> CommandResult:
        val = args.strip().lower()
        if val and val not in {"on", "off"}:
            return CommandResult(ok=False, message="streaming 用法: /streaming [on|off](无参数=切换)")
        if val:
            new_val = val == "on"
        else:
            if self.session_config_store is None:
                return CommandResult(ok=False, message="session_config_store 未配置")
            current = await self.session_config_store.get(inbound.session_id)
            new_val = not current.streaming
        new = await self._persist_with(inbound.session_id, streaming=new_val)
        return CommandResult(ok=True, message=f"streaming={new.streaming}")

    async def _cmd_thinking(self, inbound: InboundMessage, args: str) -> CommandResult:
        val = args.strip().lower()
        if val and val not in {"on", "off"}:
            return CommandResult(ok=False, message="thinking 用法: /thinking [on|off](无参数=切换)")
        if val:
            new_val = val == "on"
        else:
            if self.session_config_store is None:
                return CommandResult(ok=False, message="session_config_store 未配置")
            current = await self.session_config_store.get(inbound.session_id)
            new_val = not current.thinking
        new = await self._persist_with(inbound.session_id, thinking=new_val)
        return CommandResult(ok=True, message=f"thinking={new.thinking}")


    async def _cmd_view(self, inbound: InboundMessage, args: str) -> CommandResult:
        """/view [detailed|grouped|summary|default]:本会话的工具过程显示模式;default = 渠道默认。"""
        target = args.strip().lower()
        if not target:
            cfg = await self._current_config(inbound.session_id)
            cur = (cfg.view_mode if cfg and cfg.view_mode else None) or "渠道默认"
            return CommandResult(
                ok=True, message=f"当前显示模式: {cur}(detailed 详细 / grouped 分组 / summary 摘要)",
                payload={"buttons": _option_buttons("/view", _VIEW_MODES + ("default",), per_row=4)})
        if target not in _VIEW_MODES + ("default",):
            return CommandResult(ok=False, message=f"未知显示模式 {target}")
        new = await self._persist_with(inbound.session_id, view_mode=None if target == "default" else target)
        return CommandResult(ok=True, message=f"显示模式切换为 {new.view_mode or '渠道默认'},下一条消息生效")

    async def _cmd_search_provider(self, inbound: InboundMessage, args: str) -> CommandResult:
        target = args.strip()
        if not target:
            if not self.available_search_providers:
                return CommandResult(ok=True, message="当前由 xana-search 搜索服务负责搜索,没有可切换的搜索引擎")
            cfg = await self._current_config(inbound.session_id)
            cur = (cfg.search_provider if cfg and cfg.search_provider else None) or self.available_search_providers[0]
            return CommandResult(
                ok=True, message=f"当前搜索引擎: {cur}",
                payload={"buttons": _option_buttons("/search_provider", self.available_search_providers)})
        if not self.available_search_providers:
            return CommandResult(ok=False, message="当前由 xana-search 搜索服务负责搜索,没有可切换的搜索引擎")
        if target not in self.available_search_providers:
            return CommandResult(ok=False, message=f"未知搜索引擎 {target},可选:{', '.join(self.available_search_providers)}")
        new = await self._persist_with(inbound.session_id, search_provider=target)
        return CommandResult(ok=True, message=f"搜索引擎切换为 {new.search_provider}")

    async def _cmd_cancel(self, inbound: InboundMessage, args: str) -> CommandResult:
        if self.gateway is None:
            return CommandResult(False, "gateway 未注入")
        await self.gateway.cancel(inbound.session_id, args.strip() or None)
        return CommandResult(True, "已请求停止当前回答")

    async def _cmd_resume(self, inbound: InboundMessage, args: str) -> CommandResult:
        """/resume(无参) → 列出当前 session 可恢复的 run;/resume <run_id> → 续跑。"""
        if self.gateway is None or getattr(self.gateway, "run_store", None) is None:
            return CommandResult(ok=False, message="gateway/run_store 未注入")
        rid = (args or "").strip()
        if not rid:
            records = await self.gateway.run_store.list_unfinished()
            recs = [r for r in records if r.session_id == inbound.session_id]
            if not recs:
                return CommandResult(ok=True, message="无可恢复的 run")
            lines = ["可恢复的 run:"]
            for r in recs:
                label = (r.input_request.get("text") or "")[:40] or "(无 label)"
                reason = r.exit_reason or r.status
                lines.append(f"  {r.run_id}  · {reason}  · {label}")
            lines.append("\n用 /resume <run_id> 续跑")
            return CommandResult(ok=True, message="\n".join(lines))
        await self.gateway.resume(inbound.session_id, rid)
        return CommandResult(ok=True, message=f"[resume] 续跑 {rid}")

    async def _cmd_tasks(self, inbound: InboundMessage, args: str) -> CommandResult:
        """/tasks [session_id] → 任务面板(§4.4):run_id/状态/运行时长/label。

        数据面 = gateway.list_inflight():TurnCoordinator 活动状态与 RunStore 持久状态合并。
        cli daemon 渲染文本即可;feishu/tg 卡片后续接入同一数据面。
        """
        gateway = self.gateway
        if gateway is None or not hasattr(gateway, "list_inflight"):
            return CommandResult(ok=False, message="gateway 未注入")
        sid = args.strip() or None
        rows = await gateway.list_inflight(sid)
        if not rows:
            return CommandResult(ok=True, message="没有 run 记录")
        lines = ["run_id · 状态 · 时长 · 标签"]
        for r in sorted(rows, key=lambda r: (r.session_id, r.run_id)):
            dur = f"{int(r.duration_s)}s" if r.duration_s is not None else "-"
            activity = " [执行中]" if r.live else ""
            lines.append(f"{r.run_id} · {r.status} · {dur} · {r.label}{activity}  ({r.session_id})")
        return CommandResult(ok=True, message="\n".join(lines))

    async def _cmd_project(self, inbound: InboundMessage, args: str) -> CommandResult:
        """/project:显示当前项目(只读)。一个 serve 就是一个项目,会话不能换项目
        (docs/design/project-design.md §5);新建项目用 `xana project create`。"""
        if args.strip():
            return CommandResult(ok=False, message="会话不能切换项目:每个项目是一个独立的 serve。"
                                                   "新建项目用 `xana project create <id> --root <目录>`")
        try:
            root = str(project_root())
        except LookupError as exc:
            root = f"(未注册:{exc})"
        return CommandResult(ok=True, message=f"当前项目: {project_id()}\n项目目录: {root}")
