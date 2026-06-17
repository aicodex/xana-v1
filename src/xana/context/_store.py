"""Private in-memory store for ContextSDK (§3 + §1.5).

Only ``xana.context.sdk`` may import this module. The caller guard
mirrors ``_internal.py``.

Holds per-(sid, agent) ordered Message lists and per-sid coordination
primitives:
  - write_lock      : serializes append/replace mutations (§1.1)
  - compact_lock    : enforces "<=1 compact running per sid" (§1.5 c4)
  - compact_event   : signals "compact PH replaced" so blocked
                       snapshot_for_fork() callers wake (§1.5 c3)
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field

from xana.context.message import (
    CompactPlaceholder,
    Message,
    RegularPlaceholder,
)

_ALLOWED_CALLERS = {"xana.context.sdk"}


def _find_real_caller() -> str:
    frame = sys._getframe(1)
    while frame is not None:
        name = frame.f_globals.get("__name__", "")
        if name and name != __name__ and not name.startswith("importlib"):
            return name
        frame = frame.f_back
    return ""


_caller = _find_real_caller()
if _caller not in _ALLOWED_CALLERS:
    raise ImportError(
        f"xana.context._store is private — only ContextSDK may import. "
        f"caller={_caller!r}"
    )


@dataclass
class _SidState:
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    compact_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    compact_event: asyncio.Event = field(default_factory=asyncio.Event)
    # holder identifier (run_id) of the in-flight compact, or "" if none.
    compact_holder: str = ""


class _ContextStore:
    """Per-(sid, agent) ordered Message buffers + per-sid sync primitives."""

    def __init__(self) -> None:
        self._buffers: dict[tuple[str, str], list[Message]] = defaultdict(list)
        self._sid_state: dict[str, _SidState] = {}

    # ---- internal helpers -------------------------------------------------

    def state(self, sid: str) -> _SidState:
        st = self._sid_state.get(sid)
        if st is None:
            st = _SidState()
            # mark event as "set" by default: no pending compact = no block.
            st.compact_event.set()
            self._sid_state[sid] = st
        return st

    def buffer(self, sid: str, agent: str) -> list[Message]:
        return self._buffers[(sid, agent)]

    # ---- placeholder index helpers ---------------------------------------

    @staticmethod
    def earliest_regular_index(buf: list[Message]) -> int:
        for i, m in enumerate(buf):
            if isinstance(m, RegularPlaceholder):
                return i
        return -1

    @staticmethod
    def find_compact_placeholder_index(buf: list[Message]) -> int:
        for i, m in enumerate(buf):
            if isinstance(m, CompactPlaceholder):
                return i
        return -1

    @staticmethod
    def find_by_run_id(buf: list[Message], run_id: str) -> int:
        for i, m in enumerate(buf):
            if isinstance(m, (RegularPlaceholder, CompactPlaceholder)):
                if m._internal.run_id == run_id:
                    return i
        return -1
