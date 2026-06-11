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
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    project_root = (args.project or Path.cwd()).resolve()
    boot_result = await boot(project_root=project_root)
    adapter = CliAdapter(adapter_id="cli", session_id=args.session)
    await adapter.start()
    await boot_result.gateway.register_adapter("cli", adapter)

    text = " ".join(args.text).strip()
    if not text:
        print("[xanac] (empty input — pass text positionally)", file=sys.stderr)
        return 2

    inbound = InboundMessage(adapter_id="cli", session_id=args.session, text=text)
    exit_code = 0
    async for rendered in boot_result.gateway.submit(inbound):
        if rendered.metadata.get("event_kind") == "error":
            exit_code = 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
