"""Private context internals — ContextSDK only.

Importing this module from anywhere else than ``xana.context.sdk`` or
``xana.context._store`` raises ImportError. This is the §1.9 路线 3
mechanism that keeps internal metadata (run_id / ph_id / tool call_id /
transcript_path / debug fields) from leaking into prompt-building code.
"""
import sys
from dataclasses import dataclass

_ALLOWED_CALLERS = {"xana.context.sdk", "xana.context._store"}


def _find_real_caller() -> str:
    """Walk up the stack past importlib frames to find the actual importer."""
    frame = sys._getframe(1)
    while frame is not None:
        name = frame.f_globals.get("__name__", "")
        # Skip our own module + importlib machinery
        if name and name != __name__ and not name.startswith("importlib"):
            return name
        frame = frame.f_back
    return ""


_caller = _find_real_caller()
if _caller not in _ALLOWED_CALLERS:
    raise ImportError(
        f"xana.context._internal is private — only ContextSDK may import. "
        f"caller={_caller!r}"
    )


@dataclass(frozen=True)
class _PlaceholderInternal:
    run_id: str
    ph_id: str
    transcript_path: str = ""  # for CompactSummary template


@dataclass(frozen=True)
class _ToolCallInternal:
    call_id: str
    name: str
    arguments: dict
