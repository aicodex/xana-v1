"""AdapterProgressSink: route harness ProgressEvents into adapters.

Only `usage_progress` payloads are forwarded. Other kinds (e.g. internal
checkpoint hooks) are dropped silently. Adapter must expose `note_progress`
to opt in; adapters without it are skipped.
"""
from __future__ import annotations

from typing import Mapping

from xana.harness_sdk.contracts import ProgressEvent
from xana.harness_sdk.interfaces import ProgressSink


class AdapterProgressSink(ProgressSink):
    def __init__(self, adapters: Mapping[str, object]) -> None:
        self.adapters = adapters

    async def report(self, event: ProgressEvent) -> None:
        if event.payload.get("kind") != "usage_progress":
            return
        for adapter in self.adapters.values():
            note = getattr(adapter, "note_progress", None)
            if note is not None:
                note(event.payload)
