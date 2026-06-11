"""xana — top-level CLI dispatcher.

Subcommands:
  setup    Materialize .xana/ skeleton and initialize ~/.xana/config/system.json.

xanac (chat client) remains a separate entry point — this CLI is for
machine/project administration, not interactive chat.
"""
from __future__ import annotations

import argparse
import sys
from typing import TextIO

from xana.cli.commands import setup as setup_cmd


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xana", description="xana runtime CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser(
        "setup", help="initialize .xana/ in the current project + ~/.xana/ config"
    )
    setup_cmd.add_arguments(setup_parser)
    setup_parser.set_defaults(func=setup_cmd.run)

    return parser


def main(argv: list[str] | None = None, *, out: TextIO | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args, out=out if out is not None else sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
