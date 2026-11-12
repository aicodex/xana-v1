"""Forward user-visible run progress through the channel contract."""
from typing import Mapping
from xana.agent_adapter.interfaces import Adapter
from xana.harness_sdk.contracts import ProgressEvent
from xana.harness_sdk.interfaces import ProgressSink

_FORWARDED_KINDS = frozenset({"usage_progress", "compact_started", "compact_progress", "compact_finalized"})

class AdapterProgressSink(ProgressSink):
    def __init__(self, adapters: Mapping[str, Adapter]) -> None:
        self.adapters = adapters

    async def report(self, event: ProgressEvent) -> None:
        if event.payload.get("kind") not in _FORWARDED_KINDS:
            return
        payload = {**event.payload, "session_id": event.session_id, "run_id": event.run_id}
        for adapter in self.adapters.values():
            adapter.note_progress(payload)
