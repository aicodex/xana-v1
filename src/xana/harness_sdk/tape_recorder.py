"""No-op-capable Tape recorder for Harness compaction and stop/continuation decisions."""
from __future__ import annotations

import uuid

from xana.harness_sdk.contracts import (
    CompactionContext,
    CompactionDecision,
    StopDecision,
)
from xana.protocol.primitives import ComponentVersion
from xana.protocol.tape import TapeWriter


class HarnessTapeRecorder:
    """Writes `harness.compaction` and `harness.stop_decision` events to Stage 0 `TapeWriter`."""

    def __init__(self, writer: TapeWriter | None = None) -> None:
        self._writer = writer

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    def record_compaction(
        self,
        *,
        session_id: str,
        run_id: str,
        ctx: CompactionContext,
        decision: CompactionDecision,
        changed: bool | None = None,
        policy: ComponentVersion | None = None,
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "harness.compaction",
            actor="harness",
            provenance="internal",
            trace_id=run_id,
            span_id=f"hcmp:{uuid.uuid4().hex[:12]}",
            payload={
                "session_id": session_id,
                "action": decision.action,
                "reason": decision.reason,
                "effective_tokens": decision.effective_tokens,
                "force_compact": ctx.force_compact,
                "blocking_limit": ctx.blocking_limit,
                "changed": changed,
                "policy_version": policy.policy_version if policy else None,
            },
        )

    def record_stop_decision(
        self,
        *,
        session_id: str,
        run_id: str,
        decision: StopDecision,
        policy: ComponentVersion | None = None,
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "harness.stop_decision",
            actor="harness",
            provenance="internal",
            trace_id=run_id,
            span_id=f"hstp:{uuid.uuid4().hex[:12]}",
            payload={
                "session_id": session_id,
                "action": decision.action,
                "note_kind": decision.note_kind,
                "reason": decision.reason,
                "complete_goal": decision.complete_goal,
                "pause_goal_reason": decision.pause_goal_reason,
                "policy_version": policy.policy_version if policy else None,
            },
        )
