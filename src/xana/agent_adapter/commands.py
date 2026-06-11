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


class DefaultCommandRouter(CommandRouter):
    def __init__(
        self,
        *,
        session_config_store: Any = None,  # SessionConfigStore | None — typed loose to avoid cycles in tests
        active_context_store: Any = None,
        gateway: Any = None,
        available_models: tuple[str, ...] = (),
        available_search_providers: tuple[str, ...] = (),
    ) -> None:
        self.session_config_store = session_config_store
        self.active_context_store = active_context_store
        self.gateway = gateway
        self.available_models = available_models
        self.available_search_providers = available_search_providers

        self._specs: tuple[CommandSpec, ...] = (
            CommandSpec(name="/help",     description="显示命令菜单。用法 `/help` 或 `/help /命令`"),
            CommandSpec(name="/clear",    description="清空当前会话上下文"),
            CommandSpec(name="/compact",  description="压缩本会话历史成一条 summary"),
            CommandSpec(name="/model",    description="切换本会话模型，无参列出可选"),
            CommandSpec(name="/effort",   description="推理强度 low/medium/high"),
            CommandSpec(name="/streaming", description="token 流开关 on/off"),
            CommandSpec(name="/thinking", description="思考链开关 on/off"),
            CommandSpec(name="/attach",   description="切换 task 订阅"),
            CommandSpec(name="/cancel",   description="取消当前 turn"),
            CommandSpec(name="/project",  description="项目管理"),
            CommandSpec(name="/search_provider", description="搜索引擎切换"),
        )
        self._handlers: dict[str, _HandlerFn] = {
            "/help":     DefaultCommandRouter._cmd_help,
            "/clear":    DefaultCommandRouter._cmd_clear,
            "/compact":  DefaultCommandRouter._cmd_compact,
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
        return CommandResult(ok=True, message="\n".join(lines))

    async def _cmd_clear(self, inbound: InboundMessage, args: str) -> CommandResult:
        if self.active_context_store is None:
            return CommandResult(ok=False, message="active_context_store 未配置")
        await self.active_context_store.clear(inbound.session_id, "chat")
        return CommandResult(ok=True, message="[cleared] 上下文已清空")

    async def _cmd_compact(self, inbound: InboundMessage, args: str) -> CommandResult:
        if self.active_context_store is None:
            return CommandResult(ok=False, message="active_context_store 未配置")
        summary = args.strip() or "(用户触发的压缩 — 无具体摘要)"
        await self.active_context_store.compact(inbound.session_id, "chat", summary)
        return CommandResult(ok=True, message="[compacted] 历史已压缩")
