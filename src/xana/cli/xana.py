"""xana — top-level CLI dispatcher.

Subcommands:
  setup    First-time install: system.json + sync + launchers + PATH.
  sync     Refresh ~/.xana/code/ + ~/.xana/mem/ from the xana source.
  pair     Manage channel user pairing (list/approve/reject/revoke).
  project  Manage projects: create / list (one serve and one bot per project).
  login    Kimi OAuth device-flow login (registers k3 / kimi-for-coding).
  logout   Remove stored Kimi credentials.

xanac (chat client) is a separate entry point — this CLI is for machine
administration, not interactive chat.
"""
from __future__ import annotations

import argparse
import sys
from typing import TextIO

from xana.cli.commands import agents as agents_cmd
from xana.cli.commands import login as login_cmd
from xana.cli.commands import pair as pair_cmd
from xana.cli.commands import project as project_cmd
from xana.cli.commands import setup as setup_cmd
from xana.cli.commands import sync as sync_cmd


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xana", description="xana runtime CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    agents_parser = subparsers.add_parser("agents", help="install and run portable agent capability packages")
    agents_cmd.add_arguments(agents_parser)
    agents_parser.set_defaults(func=agents_cmd.run)

    setup_parser = subparsers.add_parser(
        "setup", help="initialize ~/.xana/ (config + code + mem + bin + PATH)"
    )
    setup_cmd.add_arguments(setup_parser)
    setup_parser.set_defaults(func=setup_cmd.run)

    sync_parser = subparsers.add_parser(
        "sync", help="refresh ~/.xana/code/ and ~/.xana/mem/ from the xana source"
    )
    sync_cmd.add_arguments(sync_parser)
    sync_parser.set_defaults(func=sync_cmd.run)

    serve_parser = subparsers.add_parser(
        "serve", help="boot channel adapters and serve messages through the gateway"
    )
    serve_parser.add_argument("--project", default=None, help="项目 id(缺省 default)")
    serve_parser.add_argument("--agent", default="chat", help="默认入口 agent")
    serve_parser.set_defaults(func=_run_serve)

    pair_parser = subparsers.add_parser(
        "pair", help="manage channel user pairing (list/approve/reject/revoke)"
    )
    pair_cmd.add_arguments(pair_parser)
    pair_parser.set_defaults(func=pair_cmd.run)

    project_parser = subparsers.add_parser(
        "project", help="manage projects (one serve and one bot per project)"
    )
    project_cmd.add_arguments(project_parser)
    project_parser.set_defaults(func=project_cmd.run)

    login_parser = subparsers.add_parser(
        "login", help="kimi OAuth device-flow login (k3 / kimi-for-coding, 1M ctx)"
    )
    login_cmd.add_arguments(login_parser)
    login_parser.set_defaults(func=login_cmd.run)

    logout_parser = subparsers.add_parser(
        "logout", help="remove stored kimi credentials"
    )
    logout_parser.set_defaults(func=login_cmd.run_logout)

    return parser


def _run_serve(args: argparse.Namespace, *, out=None) -> int:  # noqa: ARG001
    from xana.cli.serve import main as serve_main
    argv = ["--project", args.project] if args.project else []
    if args.agent != "chat":
        argv += ["--agent", args.agent]
    return serve_main(argv)


def main(argv: list[str] | None = None, *, out: TextIO | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args, out=out if out is not None else sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
