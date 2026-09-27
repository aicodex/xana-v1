"""Optional debug log for /compact + auto-compact lock/event transitions.

Enabled by environment variable ``XANA_DEBUG_COMPACT`` (``1``/``true``/
``on``/``yes``). When disabled the helpers are zero-cost (single env
read + return).

Default log file: ``~/.xana/logs/xana_debug_compact.log``. Override with
``XANA_DEBUG_COMPACT_FILE``.

Private — only imported from ``xana.context.sdk``, ``xana.memory.
active_context``, and ``xana.harness_sdk.default``. Not part of the
public surface.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from xana.config.paths import project_home


def _debug_on() -> bool:
    return os.environ.get("XANA_DEBUG_COMPACT", "") in ("1", "true", "on", "yes")


def _debug_path() -> Path:
    override = os.environ.get("XANA_DEBUG_COMPACT_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return project_home() / "logs" / "xana_debug_compact.log"


def _emit(line: str) -> None:
    if not _debug_on():
        return
    path = _debug_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    ts = datetime.now().isoformat(timespec="milliseconds")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[COMPACT] {ts} {line}\n")
    except OSError:
        pass


def log_lock(action: str, sid: str, run_id: str, caller: str) -> None:
    """action ∈ {acquire, release, leak_release, skip_release}."""
    _emit(f"LOCK {action} sid={sid} run={run_id} caller={caller}")


def log_event(action: str, sid: str, run_id: str, reason: str) -> None:
    """action ∈ {clear, set}."""
    _emit(f"EVENT {action} sid={sid} run={run_id} reason={reason}")


def log_auto_compact(sid: str, run_id: str, **fields) -> None:
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    _emit(f"AUTO sid={sid} run={run_id} {parts}")
