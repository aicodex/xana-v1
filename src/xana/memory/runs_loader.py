"""Load a per-run JSONL file into MemoryMessages for PH replacement.

The JSONL format is produced by xana.state.file_runs.RunJsonlWriter which
delegates to xana.memory.file_history._row_to_json. Each line is a JSON
object shaped like::

    {"row_id": ..., "session_id": ..., "agent_name": ...,
     "kind": "user|assistant|tool|system|compact",
     "ts": ..., "payload": {"role": ..., "content": ..., "metadata": {...}},
     "metadata": {...}}

The terminal marker written by RunJsonlWriter.finalize is::

    {"marker": "run_completed", "status": ..., "ts": ...}

and is skipped.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from xana.memory.contracts import MemoryMessage


_logger = logging.getLogger(__name__)


def runs_jsonl_to_memory_messages(path: Path) -> tuple[MemoryMessage, ...]:
    """Read a runs/{run_id}.jsonl file, return its MemoryMessages in order.

    Skips the run_completed marker. Tolerates blank lines and corrupt rows
    (logs + skips).
    """
    path = Path(path)
    if not path.exists():
        return ()
    out: list[MemoryMessage] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            _logger.warning("runs_loader: skipping corrupt row in %s: %s", path.name, exc)
            continue
        if row.get("marker") == "run_completed":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        role = payload.get("role")
        content = payload.get("content")
        if role is None or content is None:
            continue
        out.append(
            MemoryMessage(
                role=role,
                content=content,
                metadata=payload.get("metadata") or {},
            )
        )
    return tuple(out)
