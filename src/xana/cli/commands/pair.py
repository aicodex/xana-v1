# src/xana/cli/commands/pair.py
"""xana pair — manage channel user pairing (allowlist).

Pairing file lives next to channel bindings: $XANA_HOME/channels/pairings.json
(default ~/.xana/channels/pairings.json), shared with the running serve
daemon via PairingStore's fingerprint hot reload — no restart needed after
approve.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from xana.config.paths import project_home
from xana.agent_adapter.channels._pairing import PairingStore


def pairings_path() -> Path:
    return project_home() / "channels" / "pairings.json"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="show allowed + pending users").add_argument(
        "--platform", default=None
    )

    for action in ("approve", "reject", "revoke"):
        p = sub.add_parser(action, help=f"{action} a user")
        p.add_argument("platform", choices=("telegram", "feishu"))
        p.add_argument("user_id")
        if action == "approve":
            p.add_argument("--note", default="")


def run(args: argparse.Namespace, *, out: TextIO) -> int:
    store = PairingStore(path=pairings_path())

    if args.action == "list":
        allowed = store.list_allowed(platform=args.platform)
        pending = store.list_pending(platform=args.platform)
        print(f"[xana pair] file: {pairings_path()}", file=out)
        print(f"[xana pair] allowed ({len(allowed)}):", file=out)
        for plat, e in allowed:
            print(f"  v {plat:<10} {e['user_id']:<28} {e.get('approved_at', '')} {e.get('note', '')}", file=out)
        print(f"[xana pair] pending ({len(pending)}):", file=out)
        for plat, e in pending:
            print(f"  ? {plat:<10} {e['user_id']:<28} {e.get('first_seen', '')} {e.get('first_message', '')!r}", file=out)
        return 0

    if args.action == "approve":
        if store.approve(args.platform, args.user_id, note=args.note):
            print(f"[xana pair] approved {args.platform}:{args.user_id}", file=out)
            return 0
        print(f"[xana pair] no pending entry {args.platform}:{args.user_id}", file=out)
        return 1

    if args.action == "reject":
        if store.reject(args.platform, args.user_id):
            print(f"[xana pair] rejected {args.platform}:{args.user_id}", file=out)
            return 0
        print(f"[xana pair] no pending entry {args.platform}:{args.user_id}", file=out)
        return 1

    if store.revoke(args.platform, args.user_id):
        print(f"[xana pair] revoked {args.platform}:{args.user_id}", file=out)
        return 0
    print(f"[xana pair] not in allowed list: {args.platform}:{args.user_id}", file=out)
    return 1
