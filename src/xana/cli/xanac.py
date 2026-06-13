"""xanac: xana's CLI client.

Boots the runtime in-process, registers a CliAdapter, sends the user's
message through the AgentGateway, prints streamed events to stdout,
exits when the turn completes.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from xana.agent_adapter.channels._cli_renderer_stdout import StdoutRenderer
from xana.agent_adapter.cli import CliAdapter
from xana.agent_adapter.contracts import InboundMessage
from xana.cli.serve import _xana_home
from xana.runtime.boot import boot


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="xanac", description="xana CLI client")
    parser.add_argument("text", nargs="*", help="message to send (positional)")
    parser.add_argument("--project", type=Path, default=None, help="project root (default: cwd)")
    parser.add_argument("--session", default="cli-session", help="session id")
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="enter REPL mode (default when no positional text)")
    return parser.parse_args(argv)


async def _build_pipeline(args: argparse.Namespace, renderer):
    project_root = (args.project or Path.cwd()).resolve()
    boot_result = await boot(
        project_root=project_root,
        history_root=_xana_home() / "sessions",
    )
    adapter = CliAdapter(
        adapter_id="cli",
        session_id=args.session,
        renderer=renderer,
    )
    await adapter.start()
    await boot_result.gateway.register_adapter("cli", adapter)
    return boot_result, adapter, project_root


async def _run(args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    # Default mode: oneshot if user provided text OR stdin is piped;
    # otherwise launch Textual TUI.
    use_textual = (
        not text
        and not args.interactive
        and sys.stdin.isatty()
    )
    if args.interactive:
        use_textual = True

    if use_textual:
        return await _textual(args)

    renderer = StdoutRenderer()
    boot_result, adapter, _root = await _build_pipeline(args, renderer)
    try:
        if not text:
            # Fallback to legacy prompt_toolkit REPL when stdin is piped /
            # there's still no text (rare — keeps old behavior for scripts).
            print(f"xanac · interactive · session={args.session}")
            print(f"model={boot_result.model}  provider={boot_result.provider}")
            return await _repl_loop(
                boot_result.gateway, adapter=adapter, session_id=args.session
            )
        inbound = InboundMessage(adapter_id="cli", session_id=args.session, text=text)
        exit_code = 0
        async for rendered in boot_result.gateway.submit(inbound):
            if rendered.metadata.get("event_kind") == "error":
                exit_code = 1
        return exit_code
    finally:
        if boot_result.main_writer is not None:
            await boot_result.main_writer.aclose()


async def _textual(args: argparse.Namespace) -> int:
    from xana.agent_adapter.channels._cli_renderer_textual import TextualRenderer
    from xana.cli.xanac_textual import XanacApp

    renderer = TextualRenderer()
    boot_result, adapter, _root = await _build_pipeline(args, renderer)
    try:
        app = XanacApp(
            gateway=boot_result.gateway,
            session_id=args.session,
            adapter_id=adapter.adapter_id,
        )
        renderer.attach(app)
        await app.run_async()
        return 0
    finally:
        if boot_result.main_writer is not None:
            await boot_result.main_writer.aclose()


from typing import Callable, Protocol


class _PromptLike(Protocol):
    async def prompt_async(self, *args, **kwargs) -> str: ...


class _NullCtx:
    def __enter__(self) -> None: ...
    def __exit__(self, *a) -> None: ...


async def _drain(stream) -> None:
    async for _ in stream:
        pass


async def _repl_loop(
    gateway,
    *,
    adapter=None,
    prompt_factory: Callable[[], _PromptLike] | None = None,
    session_id: str = "cli-session",
    adapter_id: str = "cli",
) -> int:
    """Interactive REPL: read a line, submit, stream events, loop.

    Ctrl-C on an in-flight turn cancels via gateway.cancel(); Ctrl-C at the
    prompt or Ctrl-D exits cleanly. EOFError from prompt_factory also exits.
    """
    if prompt_factory is None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout

        def _bottom_toolbar() -> str:
            if adapter is None:
                return "ready"
            tabs = adapter.format_tabs()
            p = adapter.last_progress
            if p:
                base = (
                    f"tokens: {p.get('tokens_in', 0)} in / {p.get('tokens_out', 0)} out · "
                    f"elapsed: {p.get('elapsed_s', 0):.1f}s"
                )
            else:
                base = "ready"
            return f"{base}  ||  {tabs}" if tabs else base

        from prompt_toolkit.key_binding import KeyBindings
        kb = KeyBindings()

        @kb.add("tab", eager=True)
        def _switch_run(event) -> None:
            if adapter is None or not adapter.bg_runs:
                return
            target_rid, target_lbl = next(iter(adapter.bg_runs.items()))
            cur_rid = adapter.foreground_run_id
            adapter.set_foreground(target_rid)
            if cur_rid:
                adapter.bg_runs[cur_rid] = cur_rid
            switch = getattr(gateway, "switch_foreground", None)
            if switch is not None:
                import asyncio as _a
                _a.create_task(switch(session_id, target_rid))

        prompt = PromptSession("> ", bottom_toolbar=_bottom_toolbar, key_bindings=kb)
        prompt_ctx = patch_stdout()
    else:
        prompt = prompt_factory()
        prompt_ctx = _NullCtx()

    with prompt_ctx:
        while True:
            try:
                line = (await prompt.prompt_async()).strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if not line:
                continue
            inbound = InboundMessage(adapter_id=adapter_id, session_id=session_id, text=line)
            stream = gateway.submit(inbound)
            inflight = asyncio.create_task(_drain(stream))
            try:
                await inflight
            except KeyboardInterrupt:
                inflight.cancel()
                await gateway.cancel(session_id, None)
                print("\n[xanac] ⏹ 已取消当前 turn。")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
