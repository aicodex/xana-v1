"""Standalone CLI for xana.protocol (tape inspect / slice / verify / diff).

Strictly Python stdlib-only. Runs with zero business dependencies.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from xana.protocol.tape import (
    diff_tapes,
    read_tape,
    redact_tape_events,
    slice_tape,
)
from xana.protocol.versioning import canonical_json


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m xana.protocol")
    sub = parser.add_subparsers(dest="domain", required=True)
    tape_parser = sub.add_parser("tape", help="Inspect, slice, verify, or diff tape bundles")
    tape_sub = tape_parser.add_subparsers(dest="action", required=True)

    p_inspect = tape_sub.add_parser("inspect", help="Summarize a tape bundle")
    p_inspect.add_argument("bundle", type=Path)

    p_verify = tape_sub.add_parser("verify", help="Verify tape integrity and blobs")
    p_verify.add_argument("bundle", type=Path)

    p_slice = tape_sub.add_parser("slice", help="Slice events from a tape bundle")
    p_slice.add_argument("bundle", type=Path)
    p_slice.add_argument("--from-seq", type=int, default=1)
    p_slice.add_argument("--to-seq", type=int, default=None)
    p_slice.add_argument("--turn-id", type=str, default=None)
    p_slice.add_argument("--span-id", type=str, default=None)
    p_slice.add_argument("--type", dest="types", action="append", default=None)
    p_slice.add_argument("--redact", action="store_true")

    p_diff = tape_sub.add_parser("diff", help="Diff two tape bundles")
    p_diff.add_argument("left", type=Path)
    p_diff.add_argument("right", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if args.domain == "tape":
        if args.action == "inspect":
            res = read_tape(args.bundle, verify_blobs=False)
            type_counts: dict[str, int] = {}
            for ev in res.events:
                type_counts[ev.type] = type_counts.get(ev.type, 0) + 1
            out = {
                "tape_id": res.header.tape_id,
                "session_id": res.header.session_id,
                "run_id": res.header.run_id,
                "manifest_hash": res.header.manifest_hash,
                "status": res.status,
                "event_count": len(res.events),
                "event_types": dict(sorted(type_counts.items())),
                "errors": list(res.errors),
            }
            print(canonical_json(out))
            return 0
        if args.action == "verify":
            res = read_tape(args.bundle, verify_blobs=True)
            out = {
                "status": res.status,
                "event_count": len(res.events),
                "errors": list(res.errors),
            }
            print(canonical_json(out))
            return 0 if res.status != "corrupted" else 1
        if args.action == "slice":
            res = read_tape(args.bundle, verify_blobs=False)
            events = slice_tape(
                res,
                from_seq=args.from_seq,
                to_seq=args.to_seq,
                turn_id=args.turn_id,
                span_id=args.span_id,
                types=frozenset(args.types) if args.types else None,
            )
            payload = redact_tape_events(events) if args.redact else list(events)
            print(canonical_json(payload))
            return 0
        if args.action == "diff":
            left = read_tape(args.left, verify_blobs=False)
            right = read_tape(args.right, verify_blobs=False)
            print(canonical_json(diff_tapes(left, right)))
            return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
