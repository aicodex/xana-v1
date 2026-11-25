"""No-op-capable Tape recorder for Evolution promotion decisions and ledger entries."""
from __future__ import annotations

import uuid

from xana.evolution.contracts import LedgerEntry, PromotionDecision
from xana.protocol.primitives import ComponentVersion
from xana.protocol.tape import TapeWriter


class EvolutionTapeRecorder:
    """Writes `eval.promotion` and `eval.ledger` events to Stage 0 `TapeWriter`."""

    def __init__(self, writer: TapeWriter | None = None) -> None:
        self._writer = writer

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    def record_promotion(
        self,
        *,
        episode_id: str,
        decision: PromotionDecision,
        manifest_hash: str = "",
        policy: ComponentVersion | None = None,
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "eval.promotion",
            actor="evolution",
            provenance="evaluator",
            trace_id=episode_id,
            span_id=f"evpro:{uuid.uuid4().hex[:12]}",
            payload={
                "verdict": decision.verdict,
                "target_mean_diff": decision.target_mean_diff,
                "heldout_mean_diff": decision.heldout_mean_diff,
                "reasons": list(decision.reasons),
                "warnings": list(decision.warnings),
                "manifest_hash": manifest_hash,
                "policy_version": policy.policy_version if policy else None,
            },
        )

    def record_ledger(self, entry: LedgerEntry) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "eval.ledger",
            actor="evolution",
            provenance="evaluator",
            trace_id=entry.episode_id,
            span_id=f"evldg:{uuid.uuid4().hex[:12]}",
            payload={
                "episode_id": entry.episode_id,
                "agent": entry.agent,
                "verdict": entry.verdict,
                "delta_vs_base": entry.delta_vs_base,
                "manifest_hash": entry.manifest_hash,
                "snapshot_id": entry.snapshot_id,
            },
        )
