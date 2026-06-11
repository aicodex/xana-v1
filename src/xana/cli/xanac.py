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

from xana.agent_adapter.cli import CliAdapter
from xana.agent_adapter.contracts import InboundMessage
from xana.runtime.boot import boot


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="xanac", description="xana CLI client")
    parser.add_argument("text", nargs="*", help="message to send (positional)")
    parser.add_argument("--project", type=Path, default=None, help="project root (default: cwd)")
    parser.add_argument("--session", default="cli-session", help="session id")
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="enter REPL mode (default when no positional text)")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    project_root = (args.project or Path.cwd()).resolve()
    boot_result = await boot(project_root=project_root)
    adapter = CliAdapter(adapter_id="cli", session_id=args.session)
    await adapter.start()
    await boot_result.gateway.register_adapter("cli", adapter)

    text = " ".join(args.text).strip()
    if not text and not args.interactive:
        # Default to interactive when no text supplied (cc-style).
        args.interactive = True

    if args.interactive:
        return await _repl_loop(boot_result.gateway, session_id=args.session)

    inbound = InboundMessage(adapter_id="cli", session_id=args.session, text=text)
    exit_code = 0
    async for rendered in boot_result.gateway.submit(inbound):
        if rendered.metadata.get("event_kind") == "error":
            exit_code = 1
    return exit_code


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
        prompt = PromptSession("> ")
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
