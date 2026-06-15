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
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from xana.agent_adapter.contracts import CommandResult, CommandSpec, InboundMessage
from xana.agent_adapter.interfaces import CommandRouter

if TYPE_CHECKING:
    from xana.session_config.contracts import SessionConfig


_HandlerFn = Callable[["DefaultCommandRouter", InboundMessage, str], Awaitable[CommandResult]]


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
        write_queue: Any = None,
    ) -> None:
        self.session_config_store = session_config_store
        self.active_context_store = active_context_store
        self.gateway = gateway
        self.available_models = available_models
        self.available_search_providers = available_search_providers
        self.available_efforts = available_efforts
        self.model_registry = model_registry
        self.provider = provider
        self.model = model
        self.progress_sink = progress_sink
        self.write_queue = write_queue
        self._compact_seq = 0
        self._compact_cancels: dict[str, asyncio.Event] = {}

        self._specs: tuple[CommandSpec, ...] = (
            CommandSpec(name="/help",     description="显示命令菜单。用法 `/help` 或 `/help /命令`"),
            CommandSpec(name="/h",        description="同 /help"),
            CommandSpec(name="/clear",    description="清空当前会话上下文"),
            CommandSpec(name="/compact",  description="用 LLM 把本会话历史压缩成摘要"),
            CommandSpec(name="/model",    description="切换本会话模型，无参列出可选"),
            CommandSpec(name="/effort",   description="推理强度切换，无参列出可选"),
            CommandSpec(name="/streaming", description="token 流开关 on/off(无参数切换)"),
            CommandSpec(name="/thinking", description="思考链开关 on/off(无参数切换)"),
            CommandSpec(name="/async",    description="异步模式开关 on/off(无参数切换)"),
            CommandSpec(name="/attach",   description="切回某个后台运行"),
            CommandSpec(name="/cancel",   description="取消当前 turn"),
            CommandSpec(name="/project",  description="项目管理"),
            CommandSpec(name="/search_provider", description="搜索引擎切换"),
            CommandSpec(name="/resume",  description="列出可恢复的后台 run,或 /resume <run_id> 续跑"),
        )
        self._handlers: dict[str, _HandlerFn] = {
            "/help":            DefaultCommandRouter._cmd_help,
            "/h":               DefaultCommandRouter._cmd_help,
            "/clear":           DefaultCommandRouter._cmd_clear,
            "/compact":         DefaultCommandRouter._cmd_compact,
            "/model":           DefaultCommandRouter._cmd_model,
            "/effort":          DefaultCommandRouter._cmd_effort,
            "/streaming":       DefaultCommandRouter._cmd_streaming,
            "/thinking":        DefaultCommandRouter._cmd_thinking,
            "/async":           DefaultCommandRouter._cmd_async,
            "/search_provider": DefaultCommandRouter._cmd_search_provider,
            "/cancel":          DefaultCommandRouter._cmd_cancel,
            "/attach":          DefaultCommandRouter._cmd_attach,
            "/project":         DefaultCommandRouter._cmd_project,
            "/resume":          DefaultCommandRouter._cmd_resume,
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

    async def _cmd_clear(self, inbound: InboundMessage, args: str) -> CommandResult:
        if self.active_context_store is None:
            return CommandResult(ok=False, message="active_context_store 未配置")
        snapshot = await self.active_context_store.get(inbound.session_id, "chat")
        n = len(snapshot.messages) if snapshot else 0
        await self.active_context_store.clear(inbound.session_id, "chat")
        # /clear should produce a TRULY fresh session — rotate the adapter's
        # binding so the next inbound from this chat resolves to a brand-new
        # session_id. Without this, /resume etc. still surface historical runs
        # from the old session. CLI-style adapters (fixed session_id, no
        # binding store) silently skip — old behaviour preserved.
        new_sid: str | None = None
        adapters = getattr(self.gateway, "adapters", {}) if self.gateway is not None else {}
        adapter = adapters.get(inbound.adapter_id) if adapters else None
        external_thread_id = inbound.metadata.get("chat_id") if inbound.metadata else None
        rotate = getattr(adapter, "rotate_session", None) if adapter is not None else None
        if rotate is not None and external_thread_id:
            try:
                new_sid = rotate(external_thread_id)
            except Exception:
                new_sid = None
        suffix = f"; 新 session={new_sid}" if new_sid else ""
        return CommandResult(ok=True, message=f"[cleared] 已清空 {n} 条历史{suffix}")

    async def _cmd_compact(self, inbound: InboundMessage, args: str) -> CommandResult:
        from xana.harness_sdk.compact import (
            SUMMARY_SECTIONS, _CompactCancelled, build_post_compact_context,
            generate_summary, messages_since_boundary, partial_summary,
            post_compact_message, revealed_tools_from_messages, summary_progress,
        )
        from xana.harness_sdk.contracts import ProgressEvent
        if self.active_context_store is None:
            return CommandResult(ok=False, message="active_context_store 未配置")
        if self.model_registry is None:
            return CommandResult(ok=False, message="model_registry 未配置,无法压缩")
        snapshot = await self.active_context_store.get(inbound.session_id, "chat")
        live = messages_since_boundary(snapshot.messages) if snapshot else ()
        if not live:
            return CommandResult(ok=False, message="无可压缩内容")
        model = self.model
        if self.session_config_store is not None:
            cfg = await self.session_config_store.get(inbound.session_id)
            if cfg.model:
                model = cfg.model
        client = await self.model_registry.get(self.provider)
        if client is None:
            return CommandResult(ok=False, message=f"provider {self.provider!r} 未注册")
        self._compact_seq += 1
        # gateway 已为命令预建卡片并通过 metadata 传入 run_id → 复用同卡;
        # 直接调用(无 metadata)时退回自产 compactcmd- 前缀。
        run_id = inbound.metadata.get("command_run_id") \
            or f"compactcmd-{inbound.session_id}-{self._compact_seq}"
        # PH-driven path: 在压缩开始前 detach 该 session 上所有 inflight run,
        # 并向对应 adapter 发 compact_started → 触发 compact 卡片渲染。
        ph_note = None
        if self.gateway is not None:
            await self.gateway.detach_foreground(inbound.session_id)
            adapters = getattr(self.gateway, "adapters", {}) or {}
            adapter = adapters.get(inbound.adapter_id)
            if adapter is not None:
                ph_note = getattr(adapter, "note_progress", None)
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

        try:
            await _emit("")  # 立即出卡:空进度条 + 取消按钮
            try:
                summary = await generate_summary(
                    client, provider=self.provider, model=model, messages=live,
                    on_progress=_emit)
            except _CompactCancelled:
                # 用户取消:停流、不写回。仍需 finalize 防止 PH 卡片永久停在 running。
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
                if ph_note is not None:
                    ph_note({
                        "kind": "compact_finalized",
                        "session_id": inbound.session_id,
                        "run_id": run_id,
                        "summary": "",
                        "error": True,
                    })
                return CommandResult(ok=False, message="压缩失败:模型未返回 <summary>,请重试")
            revealed = revealed_tools_from_messages(snapshot.messages) if snapshot else set()
            # Group H: route writeback through write_queue (ReplaceRangeItem)
            # when a queue is wired; otherwise fall back to the legacy direct
            # store.set() path so unit tests without a queue keep working.
            if self.write_queue is not None:
                from xana.memory.contracts import MemoryMessage, Placeholder
                from xana.memory.write_queue import ReplaceRangeItem
                from xana.harness_sdk.compact import (
                    COMPACT_BOUNDARY_MARKER, post_compact_message,
                )
                mixed = await self.active_context_store.snapshot(
                    inbound.session_id, "chat")
                # fork_point = index of first still-running Placeholder, else len
                fork_point = len(mixed)
                for i, item in enumerate(mixed):
                    if isinstance(item, Placeholder) and item.status == "running":
                        fork_point = i
                        break
                boundary_meta: dict = {"compact_boundary": True}
                if revealed:
                    boundary_meta["pre_compact_revealed_tools"] = sorted(revealed)
                # Single summary message carrying the post-compact preamble;
                # we stash boundary metadata onto it so revealed_tools survive.
                summary_msg = MemoryMessage(
                    role="compact",
                    content=post_compact_message(summary),
                    metadata={**boundary_meta,
                              "compact_summary": True,
                              "boundary_marker": COMPACT_BOUNDARY_MARKER},
                )
                await self.write_queue.put(ReplaceRangeItem(
                    session_id=inbound.session_id, agent_name="chat",
                    start_idx=0, end_idx=fork_point, summary=summary_msg,
                ))
            else:
                await self.active_context_store.set(build_post_compact_context(
                    session_id=inbound.session_id, agent_name="chat", summary=summary,
                    revealed_tools=sorted(revealed)))
            if ph_note is not None:
                ph_note({
                    "kind": "compact_finalized",
                    "session_id": inbound.session_id,
                    "run_id": run_id,
                    "summary": summary,
                })
            # 最终卡:正文只有满格进度条;折叠块 = 上下文里那条 post-compact 消息全文(开头+摘要+结尾)
            return CommandResult(
                ok=True,
                message="▰" * len(SUMMARY_SECTIONS),
                payload={"thinking": post_compact_message(summary),
                         "thinking_title": f"已压缩 {len(live)} 条消息"},
            )
        except Exception:
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

    async def _cmd_compact_cancel(self, inbound: InboundMessage, args: str) -> CommandResult:
        """隐藏命令 /compact_cancel <run_id>:置取消事件,静默(不渲染卡片)。"""
        run_id = args.strip()
        event = self._compact_cancels.get(run_id)
        if event is not None:
            event.set()
        return CommandResult(ok=True, message="", payload={"silent": True})

    async def _persist_with(self, session_id: str, **changes) -> "SessionConfig":
        from xana.session_config.contracts import SessionConfig
        if self.session_config_store is None:
            raise RuntimeError("session_config_store 未配置")
        current = await self.session_config_store.get(session_id)
        new = SessionConfig(
            session_id=current.session_id,
            model=changes.get("model", current.model),
            effort=changes.get("effort", current.effort),
            streaming=changes.get("streaming", current.streaming),
            thinking=changes.get("thinking", current.thinking),
            search_provider=changes.get("search_provider", current.search_provider),
            project_root=changes.get("project_root", current.project_root),
            async_mode=changes.get("async_mode", current.async_mode),
            metadata=current.metadata,
        )
        await self.session_config_store.save(new)
        return new

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
            snap = await self.active_context_store.snapshot(inbound.session_id, "chat")
            for item in snap:
                if isinstance(item, Placeholder) and item.status == "running":
                    return CommandResult(
                        ok=False, message="有后台 run 在跑,请等完成或 /cancel")
        new = await self._persist_with(inbound.session_id, async_mode=new_val)
        return CommandResult(ok=True, message=f"async={new.async_mode}")

    async def _cmd_search_provider(self, inbound: InboundMessage, args: str) -> CommandResult:
        target = args.strip()
        if not target:
            if not self.available_search_providers:
                return CommandResult(ok=True, message="未配置可选搜索引擎")
            cfg = await self._current_config(inbound.session_id)
            cur = (cfg.search_provider if cfg and cfg.search_provider else None) or "native"
            return CommandResult(
                ok=True, message=f"当前搜索引擎: {cur}",
                payload={"buttons": _option_buttons("/search_provider", self.available_search_providers)})
        if self.available_search_providers and target not in self.available_search_providers:
            return CommandResult(ok=False, message=f"未知搜索引擎 {target}")
        new = await self._persist_with(inbound.session_id, search_provider=target)
        return CommandResult(ok=True, message=f"搜索引擎切换为 {new.search_provider}")

    async def _cmd_cancel(self, inbound: InboundMessage, args: str) -> CommandResult:
        if self.gateway is None:
            return CommandResult(ok=False, message="gateway 未注入")
        await self.gateway.cancel(inbound.session_id, None)
        return CommandResult(ok=True, message="[cancel] 已请求取消当前 turn")

    async def _cmd_attach(self, inbound: InboundMessage, args: str) -> CommandResult:
        """列出当前 session 所有 running Placeholder,渲染为菜单(payload.attach_choices)。
        点击 attach_run 按钮 → adapter 调用 gateway.attach(sid, run_id),自动 detach 当前 fg。"""
        from xana.memory.contracts import Placeholder
        if self.active_context_store is None:
            return CommandResult(ok=False, message="active_context_store 未配置")
        snap = await self.active_context_store.snapshot(inbound.session_id, "chat")
        choices: list[dict[str, Any]] = []
        for m in snap:
            if isinstance(m, Placeholder) and m.status == "running":
                choices.append({"run_id": m.run_id, "label": m.label})
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

    async def _cmd_project(self, inbound: InboundMessage, args: str) -> CommandResult:
        target = args.strip()
        if not target:
            if self.session_config_store is None:
                return CommandResult(ok=True, message="未配置 project_root")
            cur = await self.session_config_store.get(inbound.session_id)
            return CommandResult(ok=True, message=f"当前 project_root: {cur.project_root or '(未设置)'}")
        if self.session_config_store is None:
            return CommandResult(ok=False, message="session_config_store 未配置")
        new = await self._persist_with(inbound.session_id, project_root=target)
        return CommandResult(ok=True, message=f"project_root 切换为 {new.project_root}")
