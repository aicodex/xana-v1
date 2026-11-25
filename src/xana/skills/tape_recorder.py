"""No-op-capable Tape recorder for Skills catalog, loading, and proposal lifecycle."""
from __future__ import annotations

import uuid

from xana.protocol.primitives import ComponentVersion
from xana.protocol.tape import TapeWriter
from xana.skills.contracts import CatalogContext, CatalogPlan, SkillProposal


class SkillTapeRecorder:
    """Writes `skill.catalog`, `skill.load`, and `skill.proposal` events to Stage 0 `TapeWriter`."""

    def __init__(self, writer: TapeWriter | None = None) -> None:
        self._writer = writer

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    def record_catalog(
        self,
        ctx: CatalogContext,
        plan: CatalogPlan,
        *,
        policy: ComponentVersion | None = None,
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "skill.catalog",
            actor="skills",
            provenance="internal",
            trace_id=ctx.run_id,
            span_id=f"skcat:{uuid.uuid4().hex[:12]}",
            payload={
                "session_id": ctx.session_id,
                "agent_name": ctx.agent_name,
                "entries": [e.name for e in plan.entries],
                "total_chars": plan.total_chars,
                "estimated_tokens": plan.estimated_tokens,
                "omitted_count": plan.omitted_count,
                "catalog_hash": plan.catalog_hash,
                "policy_version": policy.policy_version if policy else None,
            },
        )

    def record_load(
        self,
        *,
        session_id: str,
        agent_name: str,
        run_id: str,
        skill_name: str,
        ok: bool,
        latency_ms: float,
        chars: int = 0,
        trust_level: str = "",
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "skill.load",
            actor="skills",
            provenance="tool",
            trace_id=run_id,
            span_id=f"skload:{uuid.uuid4().hex[:12]}",
            latency_ms=round(latency_ms, 2),
            payload={
                "session_id": session_id,
                "agent_name": agent_name,
                "skill_name": skill_name,
                "ok": ok,
                "chars": chars,
                "trust_level": trust_level,
            },
        )

    def record_proposal(self, proposal: SkillProposal, *, action: str) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "skill.proposal",
            actor="skills",
            provenance="internal",
            trace_id="",
            span_id=f"skprop:{proposal.proposal_id[:12]}",
            payload={
                "action": action,
                "proposal_id": proposal.proposal_id,
                "skill_name": proposal.skill_name,
                "source": proposal.source,
                "status": proposal.status,
                "reason": proposal.decision_reason,
            },
        )
