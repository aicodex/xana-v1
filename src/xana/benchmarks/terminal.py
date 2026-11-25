"""Container-side entry point used by the Harbor adapter."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from xana.benchmarks.process import TrialProcess


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--instruction", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=3600)
    args = parser.parse_args()
    worker = TrialProcess(args.home, Path.cwd(), args.output.parent,
                          init={}, timeout=args.timeout)
    try:
        worker.send({"kind": "user", "text": args.instruction.read_text()})
        result = worker.receive()
        if result["kind"] != "final":
            raise RuntimeError(f"Unexpected external tool request in TB: {result}")
        args.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    finally:
        worker.close()


if __name__ == "__main__":
    main()
