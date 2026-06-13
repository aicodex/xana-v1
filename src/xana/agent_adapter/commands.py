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

from typing import Any, Awaitable, Callable

from xana.agent_adapter.contracts import CommandResult, CommandSpec, InboundMessage
from xana.agent_adapter.interfaces import CommandRouter


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
        self._compact_seq = 0

        self._specs: tuple[CommandSpec, ...] = (
            CommandSpec(name="/help",     description="显示命令菜单。用法 `/help` 或 `/help /命令`"),
            CommandSpec(name="/h",        description="同 /help"),
            CommandSpec(name="/clear",    description="清空当前会话上下文"),
            CommandSpec(name="/compact",  description="用 LLM 把本会话历史压缩成摘要"),
            CommandSpec(name="/model",    description="切换本会话模型，无参列出可选"),
            CommandSpec(name="/effort",   description="推理强度切换，无参列出可选"),
            CommandSpec(name="/streaming", description="token 流开关 on/off(无参数切换)"),
            CommandSpec(name="/thinking", description="思考链开关 on/off(无参数切换)"),
            CommandSpec(name="/attach",   description="切换 task 订阅"),
            CommandSpec(name="/cancel",   description="取消当前 turn"),
            CommandSpec(name="/project",  description="项目管理"),
            CommandSpec(name="/search_provider", description="搜索引擎切换"),
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
            "/search_provider": DefaultCommandRouter._cmd_search_provider,
            "/cancel":          DefaultCommandRouter._cmd_cancel,
            "/attach":          DefaultCommandRouter._cmd_attach,
            "/project":         DefaultCommandRouter._cmd_project,
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
        # 每个命令一个 copy 按钮(点击≠执行,只把命令名放进输入/剪贴板),2 列/行。
        copies = [{"text": spec.name, "copy": spec.name} for spec in self._specs]
        rows = [copies[i:i + 2] for i in range(0, len(copies), 2)]
        return CommandResult(ok=True, message="\n".join(lines), payload={"buttons": rows})

    async def _cmd_clear(self, inbound: InboundMessage, args: str) -> CommandResult:
        if self.active_context_store is None:
            return CommandResult(ok=False, message="active_context_store 未配置")
        snapshot = await self.active_context_store.get(inbound.session_id, "chat")
        n = len(snapshot.messages) if snapshot else 0
        await self.active_context_store.clear(inbound.session_id, "chat")
        return CommandResult(ok=True, message=f"[cleared] 已清空 {n} 条历史")

    async def _cmd_compact(self, inbound: InboundMessage, args: str) -> CommandResult:
        from xana.harness_sdk.compact import (
            SUMMARY_SECTIONS, build_post_compact_context, generate_summary,
            messages_since_boundary, partial_summary, post_compact_message,
            revealed_tools_from_messages, summary_progress,
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
        last_emitted: list[tuple[str, str, str]] = [("", "", "")]

        async def _emit(text_so_far: str = "", phase: str = "running") -> None:
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
                         "thinking_title": f"🗜 正在压缩 · {status}"},
            ))

        await _emit("")  # 立即出卡:空进度条
        summary = await generate_summary(
            client, provider=self.provider, model=model, messages=live,
            on_progress=_emit)
        if summary is None:
            return CommandResult(ok=False, message="压缩失败:模型未返回 <summary>,请重试")
        revealed = revealed_tools_from_messages(snapshot.messages) if snapshot else set()
        await self.active_context_store.set(build_post_compact_context(
            session_id=inbound.session_id, agent_name="chat", summary=summary,
            revealed_tools=sorted(revealed)))
        # 最终卡:正文只有满格进度条;折叠块 = 上下文里那条 post-compact 消息全文(开头+摘要+结尾)
        return CommandResult(
            ok=True,
            message="▰" * len(SUMMARY_SECTIONS),
            payload={"thinking": post_compact_message(summary),
                     "thinking_title": f"已压缩 {len(live)} 条消息"},
        )

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
            metadata=current.metadata,
        )
        await self.session_config_store.save(new)
        return new

    async def _cmd_model(self, inbound: InboundMessage, args: str) -> CommandResult:
        target = args.strip()
        if not target:
            if not self.available_models:
                return CommandResult(ok=True, message="未配置可选模型")
            return CommandResult(
                ok=True, message="可选模型: " + ", ".join(self.available_models),
                payload={"buttons": _option_buttons("/model", self.available_models)})
        if self.available_models and target not in self.available_models:
            return CommandResult(ok=False, message=f"未知模型 {target}; 可选: " + ", ".join(self.available_models))
        new = await self._persist_with(inbound.session_id, model=target)
        return CommandResult(ok=True, message=f"模型切换为 {new.model}")

    async def _cmd_effort(self, inbound: InboundMessage, args: str) -> CommandResult:
        target = args.strip().lower()
        opts = ", ".join(self.available_efforts)
        if not target:
            return CommandResult(
                ok=True, message=f"可选 effort: {opts}",
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

    async def _cmd_search_provider(self, inbound: InboundMessage, args: str) -> CommandResult:
        target = args.strip()
        if not target:
            if not self.available_search_providers:
                return CommandResult(ok=True, message="未配置可选搜索引擎")
            return CommandResult(
                ok=True, message="可选搜索引擎: " + ", ".join(self.available_search_providers),
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
        if self.session_config_store is None:
            return CommandResult(ok=False, message="session_config_store 未配置")
        current = await self.session_config_store.get(inbound.session_id)
        next_attached = not bool(current.metadata.get("attached"))
        from xana.session_config.contracts import SessionConfig
        new_metadata = dict(current.metadata)
        new_metadata["attached"] = next_attached
        new = SessionConfig(
            session_id=current.session_id, model=current.model, effort=current.effort,
            streaming=current.streaming, thinking=current.thinking,
            search_provider=current.search_provider, project_root=current.project_root,
            metadata=new_metadata,
        )
        await self.session_config_store.save(new)
        return CommandResult(ok=True, message=f"attached={next_attached}")

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
