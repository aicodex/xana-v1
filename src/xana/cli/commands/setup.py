"""xana setup — initialize ~/.xana/config/system.json.

Project-level .xana/ is deliberately NOT created: system.json holds API keys
and other secrets, and we keep that entirely user-level (mirrors yuxu's
~/.yuxu/config/ placement) so nothing secret can leak into a project tree
that might be committed to git.

Idempotent: re-running never overwrites an existing system.json.
"""
from __future__ import annotations

import argparse
from typing import TextIO

from xana.config.loader import ensure_user_system_json, user_config_dir


def add_arguments(parser: argparse.ArgumentParser) -> None:
    return None


def run(args: argparse.Namespace, *, out: TextIO) -> int:
    target_dir = user_config_dir()
    target_path = target_dir / "system.json"
    pre_existed = target_path.exists() and target_path.stat().st_size > 0
    user_path = ensure_user_system_json()
    print(f"[xana setup] user config dir   {target_dir}", file=out)
    print(f"  {'ready  ' if pre_existed else 'created'} {user_path.name}", file=out)
    return 0
