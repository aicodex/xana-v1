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
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from xana.config.paths import project_home, project_id, project_root
from xana.agent_adapter.contracts import CommandResult, CommandSpec, InboundMessage
from xana.agent_adapter.interfaces import CommandRouter

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
        gateway: Any = None,
        available_models: tuple[str, ...] = (),
        available_search_providers: tuple[str, ...] = (),
        available_efforts: tuple[str, ...] = ("high", "max"),
        model_registry: Any = None,
        provider: str = "",
        model: str = "",
        progress_sink: Any = None,
        default_agent: str = "chat",
        agent_registry: Any = None,   # AgentRegistry | None — /agent 前刷新(热加载)
    ) -> None:
        self.session_config_store = session_config_store
        self.default_agent = default_agent
        self.agent_registry = agent_registry
        self.active_context_store = active_context_store
        self.gateway = gateway
        self.available_models = available_models
        self.available_search_providers = available_search_providers
        self.available_efforts = available_efforts
        self.model_registry = model_registry
        self.provider = provider
        self.model = model
        self.progress_sink = progress_sink
        self._compact_seq = 0
        self._compact_cancels: dict[str, asyncio.Event] = {}

        self._specs: tuple[CommandSpec, ...] = (
            CommandSpec(name="/help",     description="显示命令菜单。用法 `/help` 或 `/help /命令`"),
            CommandSpec(name="/h",        description="同 /help"),
            CommandSpec(name="/clear",    description="清空当前会话上下文"),
            CommandSpec(name="/agent",    description="用指定 agent 开新会话,无参列出可选"),
            CommandSpec(name="/compact",  description="用 LLM 把本会话历史压缩成摘要"),
            CommandSpec(name="/model",    description="切换本会话模型，无参列出可选"),
            CommandSpec(name="/effort",   description="推理强度切换，无参列出可选"),
            CommandSpec(name="/streaming", description="token 流开关 on/off(无参数切换)"),
            CommandSpec(name="/thinking", description="思考链开关 on/off(无参数切换)"),
            CommandSpec(name="/async",    description="异步模式开关 on/off(无参数切换)"),
            CommandSpec(name="/attach",   description="切回某个后台运行"),
            CommandSpec(name="/cancel",   description="取消当前 turn"),
            CommandSpec(name="/project",  description="显示当前项目"),
            CommandSpec(name="/search_provider", description="搜索引擎切换"),
            CommandSpec(name="/view",    description="工具过程显示模式:详细 / 分组 / 摘要(无参列出可选)"),
            CommandSpec(name="/resume",  description="列出可恢复的后台 run,或 /resume <run_id> 续跑"),
            CommandSpec(name="/tasks",   description="任务面板:列出在途/后台/已完成的 run(可 /tasks <session_id> 过滤)"),
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
            "/async":           DefaultCommandRouter._cmd_async,
            "/search_provider": DefaultCommandRouter._cmd_search_provider,
            "/view":            DefaultCommandRouter._cmd_view,
            "/cancel":          DefaultCommandRouter._cmd_cancel,
            "/attach":          DefaultCommandRouter._cmd_attach,
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

    async def _cmd_compact(self, inbound: InboundMessage, args: str) -> CommandResult:
        from xana.harness_sdk.compact import (
            _CompactCancelled,
            generate_summary, partial_summary, persist_compact_transcript,
            revealed_tools_from_messages, summary_progress,
        )
        from xana.harness_sdk.contracts import ProgressEvent
        if self.active_context_store is None:
            return CommandResult(ok=False, message="active_context_store 未配置")
        if self.model_registry is None:
            return CommandResult(ok=False, message="model_registry 未配置,无法压缩")
        snapshot = await self.active_context_store.get(inbound.session_id, await self._session_agent(inbound.session_id))
        live = snapshot.messages if snapshot else ()
        if not live:
            # 空上下文(常见于 /clear 之后)→ 给个 run_id 让 gateway 渲菜单卡,
            # 否则 /compact 是 silent_cmd 不预建卡,error 路径会 card_id 空 → 用户看不到。
            self._compact_seq += 1
            empty_rid = f"compactcmd-{inbound.session_id}-{self._compact_seq}"
            return CommandResult(
                ok=False,
                message="无可压缩内容(请先发条消息再 /compact)",
                payload={"run_id": empty_rid},
            )
        model = self.model
        if self.session_config_store is not None:
            cfg = await self.session_config_store.get(inbound.session_id)
            if cfg.model:
                model = cfg.model
        client = await self.model_registry.get(self.provider)
        if client is None:
            return CommandResult(ok=False, message=f"provider {self.provider!r} 未注册")
        self._compact_seq += 1
        # PH-driven path:
        #  1) detach 所有 in-flight run
        #  2) run_id 选择:
        #     - 有 in-flight → 用 inflight[0].run_id(compact 段挂到现存 PH 卡)
        #     - 无 in-flight → 自产 compactcmd-...(新建一张 compact 卡)
        #  gateway 不为 /compact 预建命令卡(见 _submit silent_cmd),所以这里
        #  全权决定 run_id,避免重复出现"⏳ /compact"占位卡 + PH 卡两张。
        ph_note = None
        inflight_run_id: str | None = None
        if self.gateway is not None:
            await self.gateway.detach_foreground(inbound.session_id)
            inflight = list(self.gateway._inflight.get(
                inbound.session_id, []))
            if inflight:
                inflight_run_id = inflight[0].run_id
            adapters = getattr(self.gateway, "adapters", {}) or {}
            adapter = adapters.get(inbound.adapter_id)
            if adapter is not None:
                ph_note = getattr(adapter, "note_progress", None)
        run_id = (
            inflight_run_id
            or inbound.metadata.get("command_run_id")
            or f"compactcmd-{inbound.session_id}-{self._compact_seq}"
        )
        if ph_note is not None:
            ph_note({
                "kind": "compact_started",
                "session_id": inbound.session_id,
                "run_id": run_id,
                "fork_point": 0,
            })
        last_emitted: list[tuple[str, str, str]] = [("", "", "")]
        cancel_event = asyncio.Event()
        self._compact_cancels[run_id] = cancel_event
        cancel_button = [[{"text": "取消", "send": f"/compact_cancel {run_id}"}]]

        async def _emit(text_so_far: str = "", phase: str = "running") -> None:
            if cancel_event.is_set():
                raise _CompactCancelled()
            if self.progress_sink is None:
                return
            bar, status = summary_progress(text_so_far)
            partial = partial_summary(text_so_far) or ""
            if phase == "running" and (bar, status, partial) == last_emitted[0]:
                return  # 进度条/状态/部分摘要都没变 → 不发纯重复
            last_emitted[0] = (bar, status, partial)
            await self.progress_sink.report(ProgressEvent(
                session_id=inbound.session_id, run_id=run_id,
                message="compact_progress",
                payload={"kind": "compact_progress", "session_id": inbound.session_id,
                         "run_id": run_id, "model": model, "phase": phase, "text": bar,
                         "thinking": partial,
                         "thinking_title": f"🗜 正在压缩 · {status}",
                         "buttons": cancel_button},
            ))

        # §1.5 Compact-as-PH: acquire SDK compact lock before LLM call. This
        # is the race fix — once the PH is appended under the SDK's per-sid
        # write_lock+compact_lock, any concurrent ph_replaced + subsequent
        # writeback are serialized; the summary replaces ONLY the
        # CompactPlaceholder (by run_id), never "all messages including the
        # freshly-arrived answer".
        sdk = self.active_context_store.sdk()
        agent = await self._session_agent(inbound.session_id)
        try:
            await sdk.append_compact_placeholder(
                inbound.session_id, agent, run_id=run_id, ph_id=run_id,
                transcript_path=f"runs/{run_id}.jsonl",
            )
        except Exception:
            # 锁已由 SDK 在失败时释放;没有占位符就无法写回,直接报失败
            _logger.exception("/compact: append_compact_placeholder failed")
            return CommandResult(ok=False, message="压缩失败:无法开始压缩,请重试")
        sdk_ph_acquired = True
        try:
            await _emit("")  # 立即出卡:空进度条 + 取消按钮
            try:
                summary = await generate_summary(
                    client, provider=self.provider, model=model, messages=live,
                    on_progress=_emit)
            except _CompactCancelled:
                # 用户取消:停流、不写回。仍需 finalize 防止 PH 卡片永久停在 running。
                await sdk.cancel_compact_placeholder(inbound.session_id, agent, run_id)
                sdk_ph_acquired = False
                if ph_note is not None:
                    ph_note({
                        "kind": "compact_finalized",
                        "session_id": inbound.session_id,
                        "run_id": run_id,
                        "summary": "",
                        "cancelled": True,
                    })
                return CommandResult(
                    ok=False, message="🛑 压缩已取消", payload={"run_id": run_id})
            if summary is None:
                await sdk.cancel_compact_placeholder(inbound.session_id, agent, run_id)
                sdk_ph_acquired = False
                if ph_note is not None:
                    ph_note({
                        "kind": "compact_finalized",
                        "session_id": inbound.session_id,
                        "run_id": run_id,
                        "summary": "",
                        "error": True,
                    })
                return CommandResult(ok=False, message="压缩失败:模型未返回 <summary>,请重试")
            # §1.5 Compact-as-PH writeback: replace the CompactPlaceholder
            # we appended above with a CompactSummary. SDK serializes this
            # under its write_lock, so a racing ph_replaced cannot stomp on
            # the freshly-arrived answer (the previous race source — see
            # §11.3 / B3).
            # 摘要引用的"完整记录"要真实存在(与 auto-compact 同一落盘函数);压缩前已揭示的工具随摘要保留
            transcript = (persist_compact_transcript(project_home() / "sessions", inbound.session_id, live)
                          or f"runs/{run_id}.jsonl")
            revealed = tuple(sorted(revealed_tools_from_messages(snapshot.messages))) if snapshot else ()
            from xana.context._internal import _PlaceholderInternal
            from xana.context.message import CompactSummary
            cs = CompactSummary(
                summary=summary,
                _internal=_PlaceholderInternal(
                    run_id=run_id, ph_id=run_id,
                    transcript_path=transcript,
                ),
                revealed_tools=revealed,
            )
            await sdk.replace_placeholder(inbound.session_id, agent, run_id, (cs,))
            sdk_ph_acquired = False
            # §1.9 single-source render: same string the model will see
            # (CompactSummary.render, via the bridge) is what the card panel
            # shows. Without this, the card body was the raw model output
            # (no template prefix/suffix), while the model context had the
            # full templated form — visible divergence between front-end
            # and back-end "compact summary".
            from xana.context.message import CompactSummary as _CS
            rendered_summary = _CS.render(summary, transcript_path=transcript)
            if ph_note is not None:
                ph_note({
                    "kind": "compact_finalized",
                    "session_id": inbound.session_id,
                    "run_id": run_id,
                    "summary": rendered_summary,
                    "compacted_n": len(live),
                })
            # compact 完成:visual 已由 compact_finalized note_progress 落到
            # PH 卡上(compact 段切完成态)。
            #  - 用了 inflight PH 卡 → 直接 silent(避免再发一条 final 把卡覆盖)
            #  - 自建 compactcmd-... 卡 → 正常发 final 把卡 patch 为完成态
            # /compact 始终 silent:compact_finalized note_progress 已经把
            # 卡片的 compact 段切到完成态。再发 final 会让 _apply_agent_only 把
            # CommandResult.message ('▰▰▰▰▰▰▰') 落到 menu section,在同一张卡
            # 上出现"第二个进度条"。
            return CommandResult(
                ok=True, message="", payload={"silent": True})
        except Exception:
            if sdk_ph_acquired:
                try:
                    await sdk.cancel_compact_placeholder(inbound.session_id, agent, run_id)
                except Exception:
                    pass
                sdk_ph_acquired = False
            if ph_note is not None:
                ph_note({
                    "kind": "compact_finalized",
                    "session_id": inbound.session_id,
                    "run_id": run_id,
                    "summary": "",
                    "error": True,
                })
            raise
        finally:
            self._compact_cancels.pop(run_id, None)
            # Safety: if for any reason we still hold the SDK compact lock
            # (e.g. an early `return` path skipped release), let it go.
            if sdk_ph_acquired:
                try:
                    await sdk.cancel_compact_placeholder(inbound.session_id, agent, run_id)
                except Exception:
                    pass

    async def _cmd_compact_cancel(self, inbound: InboundMessage, args: str) -> CommandResult:
        """隐藏命令 /compact_cancel <run_id>:置取消事件,静默(不渲染卡片)。"""
        run_id = args.strip()
        event = self._compact_cancels.get(run_id)
        if event is not None:
            event.set()
        return CommandResult(ok=True, message="", payload={"silent": True})

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

    async def _cmd_async(self, inbound: InboundMessage, args: str) -> CommandResult:
        val = args.strip().lower()
        if val and val not in {"on", "off"}:
            return CommandResult(ok=False, message="async 用法: /async [on|off](无参数=切换)")
        if self.session_config_store is None:
            return CommandResult(ok=False, message="session_config_store 未配置")
        current = await self.session_config_store.get(inbound.session_id)
        if val:
            new_val = val == "on"
        else:
            new_val = not current.async_mode
        # on→off guard: 若仍有 running placeholder,拒绝
        if current.async_mode and not new_val and self.active_context_store is not None:
            from xana.memory.contracts import Placeholder
            snap = await self.active_context_store.snapshot(inbound.session_id, await self._session_agent(inbound.session_id))
            for item in snap:
                if isinstance(item, Placeholder) and item.status == "running":
                    return CommandResult(
                        ok=False, message="有后台 run 在跑,请等完成或 /cancel")
        new = await self._persist_with(inbound.session_id, async_mode=new_val)
        return CommandResult(ok=True, message=f"async={new.async_mode}")

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
        """/cancel <run_id> → 取消该 run。
        /cancel(无参) → 列出当前 session 所有 running Placeholder,渲染菜单。
        每项点击 send `/cancel <run_id>` 取消该后台 run(类比 /attach)。"""
        if self.gateway is None:
            return CommandResult(ok=False, message="gateway 未注入")
        rid = (args or "").strip()
        if rid:
            await self.gateway.cancel(inbound.session_id, rid)
            return CommandResult(
                ok=True, message=f"[cancel] 已请求取消 {rid}")
        # 无参 → 列出可取消的后台 run
        from xana.memory.contracts import Placeholder
        if self.active_context_store is None:
            return CommandResult(ok=False, message="active_context_store 未配置")
        snap = await self.active_context_store.snapshot(
            inbound.session_id, await self._session_agent(inbound.session_id))
        sends: list[dict[str, Any]] = []
        labels: list[str] = []
        for m in snap:
            if isinstance(m, Placeholder) and m.status == "running":
                sends.append({
                    "text": f"取消 {m.label[:30]}",
                    "send": f"/cancel {m.run_id}",
                })
                labels.append(m.label)
        if not sends:
            return CommandResult(
                ok=True, message="没有后台运行可取消",
                payload={"triggers_turn": False})
        # 一列一个按钮
        rows = [[s] for s in sends]
        text_lines = ["选择要取消的后台运行:"] + [f"  {l}" for l in labels]
        return CommandResult(
            ok=True, message="\n".join(text_lines),
            payload={"triggers_turn": False, "buttons": rows},
        )

    async def _cmd_attach(self, inbound: InboundMessage, args: str) -> CommandResult:
        """/attach(无参):列出当前 session 所有 running Placeholder,渲染为菜单(payload.attach_choices);
        菜单按钮发 /attach <run_id> → gateway.attach(sid, run_id) 切回前台(自动 detach 当前 fg)。"""
        from xana.memory.contracts import Placeholder
        if self.active_context_store is None:
            return CommandResult(ok=False, message="active_context_store 未配置")
        snap = await self.active_context_store.snapshot(inbound.session_id, await self._session_agent(inbound.session_id))
        choices: list[dict[str, Any]] = []
        for m in snap:
            if isinstance(m, Placeholder) and m.status == "running":
                choices.append({"run_id": m.run_id, "label": m.label})
        target = args.strip()
        if target:
            # 菜单按钮"切回 X"发的是 /attach <run_id>:直接切回前台,原卡片继续刷新,不再发菜单
            # (2026-10-19:以前忽略参数,点了只会再弹一次菜单)
            if self.gateway is None:
                return CommandResult(ok=False, message="gateway 未注入,无法切回")
            if target not in {c["run_id"] for c in choices}:
                return CommandResult(ok=True, message="这个后台运行已经结束,不用切回了",
                                     payload={"triggers_turn": False})
            await self.gateway.attach(inbound.session_id, target)
            return CommandResult(ok=True, message="", payload={"triggers_turn": False, "silent": True})
        if not choices:
            return CommandResult(
                ok=True, message="没有后台运行可切换",
                payload={"triggers_turn": False},
            )
        return CommandResult(
            ok=True,
            message="选择要切回前台的后台运行",
            payload={
                "triggers_turn": False,
                "requires_detach": True,
                "attach_choices": choices,
            },
        )

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

        数据面 = gateway.list_inflight():_inflight 活体 + RunStore 持久状态合并。
        cli daemon 渲染文本即可;feishu/tg 卡片后续接入同一数据面。
        """
        gateway = self.gateway
        if gateway is None or not hasattr(gateway, "list_inflight"):
            return CommandResult(ok=False, message="gateway 未注入")
        sid = args.strip() or None
        rows = await gateway.list_inflight(sid)
        if not rows:
            return CommandResult(ok=True, message="没有 run 记录",
                                 payload={"triggers_turn": False})
        lines = ["run_id · 状态 · 时长 · 标签"]
        for r in sorted(rows, key=lambda r: (r.session_id, r.run_id)):
            dur = f"{int(r.duration_s)}s" if r.duration_s is not None else "-"
            fg = "" if r.attached else " [后台]"
            lines.append(f"{r.run_id} · {r.status} · {dur} · {r.label}{fg}  ({r.session_id})")
        return CommandResult(ok=True, message="\n".join(lines),
                             payload={"triggers_turn": False})

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
