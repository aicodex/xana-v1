"""Shared path resolution for CC-style builtin file tools.

Absolute paths are used as-is. Relative paths are resolved against the
tool context's cwd (the project root the harness was booted with).
"""
from __future__ import annotations

from pathlib import Path


def resolve_path(raw: str, cwd: str) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    return (Path(cwd) / p).resolve()
