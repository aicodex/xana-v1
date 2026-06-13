"""AdapterProgressSink: route harness ProgressEvents into adapters.

Only `usage_progress` and `compact_progress` payloads are forwarded
(unchanged). Other kinds (e.g. internal checkpoint hooks) are dropped
silently. Adapter must expose `note_progress` to opt in; adapters without
it are skipped.
"""
from __future__ import annotations

from typing import Mapping

from xana.harness_sdk.contracts import ProgressEvent
from xana.harness_sdk.interfaces import ProgressSink

_FORWARDED_KINDS = ("usage_progress", "compact_progress")


class AdapterProgressSink(ProgressSink):
    def __init__(self, adapters: Mapping[str, object]) -> None:
        self.adapters = adapters

    async def report(self, event: ProgressEvent) -> None:
        if event.payload.get("kind") not in _FORWARDED_KINDS:
            return
        # Stuff run_id into the payload so adapters can route per-turn
        # (harness 没把 ProgressEvent.run_id 写进 payload; 不补的话 adapter
        # 端 usage_progress 永远落到 buf.turns 的最后一个 turn,被 detach 的 run
        # 也会被尾随刷新)。
        payload = {**event.payload, "run_id": event.run_id} if event.run_id else event.payload
        for adapter in self.adapters.values():
            note = getattr(adapter, "note_progress", None)
            if note is not None:
                note(payload)
