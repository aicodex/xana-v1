"""No-op-capable Tape recorder for Memory recall, search, write, extract, and snapshots."""
from __future__ import annotations

import uuid

from xana.memory.contracts import (
    ExtractionOutcome,
    MemorySnapshot,
    MemoryWriteOutcome,
    RetrievalContext,
    RetrievalPlan,
)
from xana.protocol.primitives import ComponentVersion
from xana.protocol.tape import TapeWriter


class MemoryTapeRecorder:
    """Writes `memory.*` events to Stage 0 `TapeWriter` when configured."""

    def __init__(self, writer: TapeWriter | None = None) -> None:
        self._writer = writer

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    def record_recall(
        self,
        ctx: RetrievalContext,
        plan: RetrievalPlan,
        *,
        policy: ComponentVersion | None = None,
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "memory.recall",
            actor="memory",
            provenance="internal",
            trace_id=ctx.run_id,
            span_id=f"memrec:{uuid.uuid4().hex[:12]}",
            latency_ms=round(plan.latency_ms, 2),
            payload={
                "session_id": ctx.session_id,
                "agent_name": ctx.agent_name,
                "mode": ctx.mode,
                "record_count": len(ctx.records),
                "block_count": len(plan.blocks),
                "recalled_paths": list(plan.recalled_paths),
                "skipped_reason": plan.skipped_reason,
                "judge_models": list(plan.judge_models),
                "policy_version": policy.policy_version if policy else None,
            },
        )

    def record_search(
        self,
        *,
        query: str,
        hit_count: int,
        latency_ms: float,
        judge_models: tuple[str, ...] = (),
        usage: dict | None = None,
        run_id: str = "",
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "memory.search",
            actor="memory",
            provenance="internal",
            trace_id=run_id,
            span_id=f"memsrc:{uuid.uuid4().hex[:12]}",
            latency_ms=round(latency_ms, 2),
            payload={
                "query_chars": len(query),
                "hit_count": hit_count,
                "judge_models": list(judge_models),
                "usage": dict(usage) if usage else {},
            },
        )

    def record_write(self, outcome: MemoryWriteOutcome, *, run_id: str = "") -> None:
        if self._writer is None:
            return
        self._writer.append(
            "memory.write",
            actor="memory",
            provenance="tool_trusted",
            trace_id=run_id,
            span_id=f"memwrt:{uuid.uuid4().hex[:12]}",
            payload={
                "action": outcome.action,
                "path": outcome.path,
                "ok": outcome.ok,
                "reason": outcome.reason,
            },
        )

    def record_extract(
        self,
        outcome: ExtractionOutcome,
        *,
        policy: ComponentVersion | None = None,
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "memory.extract",
            actor="memory",
            provenance="internal",
            trace_id=outcome.run_id,
            span_id=f"memext:{uuid.uuid4().hex[:12]}",
            latency_ms=round(outcome.latency_ms, 2),
            payload={
                "session_id": outcome.session_id,
                "written_paths": list(outcome.written_paths),
                "skipped_reason": outcome.skipped_reason,
                "error": outcome.error,
                "policy_version": policy.policy_version if policy else None,
            },
        )

    def record_snapshot(
        self,
        snapshot: MemorySnapshot,
        *,
        action: str,
        run_id: str = "",
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "memory.snapshot",
            actor="memory",
            provenance="internal",
            trace_id=run_id,
            span_id=f"memsnp:{snapshot.snapshot_id[:12]}",
            payload={
                "action": action,
                "snapshot_id": snapshot.snapshot_id,
                "entry_count": len(snapshot.entries),
            },
        )
