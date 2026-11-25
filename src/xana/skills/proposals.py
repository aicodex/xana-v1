"""InMemory and atomic File implementations of SkillProposalQueue."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal

from xana.protocol.diag import DiagLogger
from xana.skills.contracts import ProposalStatus, SkillProposal
from xana.skills.interfaces import SkillProposalQueue

_DIAG = DiagLogger("skills")


def _now_ms() -> int:
    return int(time.time() * 1000)


class InMemorySkillProposalQueue(SkillProposalQueue):
    """In-memory SkillProposalQueue for standalone tests and second-host conformance."""

    def __init__(self) -> None:
        self._items: dict[str, SkillProposal] = {}

    def submit(self, proposal: SkillProposal) -> SkillProposal:
        created = proposal.created_at_ms or _now_ms()
        stored = replace(proposal, status="pending", created_at_ms=created)
        self._items[stored.proposal_id] = stored
        _DIAG.op("proposal.submitted", id=stored.proposal_id, skill=stored.skill_name, source=stored.source)
        return stored

    def list(self, status: ProposalStatus | None = None) -> tuple[SkillProposal, ...]:
        items = self._items.values()
        if status is not None:
            items = [p for p in items if p.status == status]
        return tuple(sorted(items, key=lambda p: (p.created_at_ms, p.proposal_id)))

    def get(self, proposal_id: str) -> SkillProposal | None:
        return self._items.get(proposal_id)

    def decide(
        self,
        proposal_id: str,
        *,
        status: Literal["approved", "rejected"],
        reason: str = "",
        now_ms: int | None = None,
    ) -> SkillProposal:
        cur = self._items.get(proposal_id)
        if cur is None:
            raise LookupError(f"proposal not found: {proposal_id!r}")
        if cur.status != "pending":
            raise ValueError(f"proposal {proposal_id!r} already {cur.status}")
        updated = replace(cur, status=status, decided_at_ms=now_ms or _now_ms(), decision_reason=reason)
        self._items[proposal_id] = updated
        _DIAG.op("proposal.decided", id=proposal_id, skill=updated.skill_name, status=status)
        return updated

    def prune_expired(self, now_ms: int | None = None) -> tuple[SkillProposal, ...]:
        ts = now_ms if now_ms is not None else _now_ms()
        expired: list[SkillProposal] = []
        for pid, p in list(self._items.items()):
            if p.status == "pending" and p.expires_at_ms is not None and p.expires_at_ms <= ts:
                updated = replace(p, status="expired", decided_at_ms=ts, decision_reason="expired")
                self._items[pid] = updated
                expired.append(updated)
        if expired:
            _DIAG.anomaly("proposal.expired", count=len(expired))
        return tuple(expired)


class FileSkillProposalQueue(InMemorySkillProposalQueue):
    """Atomic JSON-backed SkillProposalQueue."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path)
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, list):
            return
        for row in raw:
            if not isinstance(row, dict) or not row.get("proposal_id"):
                continue
            self._items[row["proposal_id"]] = SkillProposal(
                proposal_id=str(row["proposal_id"]),
                skill_name=str(row.get("skill_name", "")),
                description=str(row.get("description", "")),
                body=str(row.get("body", "")),
                when_to_use=str(row.get("when_to_use", "")),
                source=str(row.get("source", "memory")),
                evidence=tuple(row.get("evidence") or ()),
                target_agents=tuple(row.get("target_agents") or ()),
                trust_level=row.get("trust_level", "local"),
                status=row.get("status", "pending"),
                created_at_ms=int(row.get("created_at_ms", 0)),
                expires_at_ms=row.get("expires_at_ms"),
                decided_at_ms=row.get("decided_at_ms"),
                decision_reason=str(row.get("decision_reason", "")),
                metadata=dict(row.get("metadata") or {}),
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(p) for p in self.list()]
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)

    def submit(self, proposal: SkillProposal) -> SkillProposal:
        res = super().submit(proposal)
        self._save()
        return res

    def decide(
        self,
        proposal_id: str,
        *,
        status: Literal["approved", "rejected"],
        reason: str = "",
        now_ms: int | None = None,
    ) -> SkillProposal:
        res = super().decide(proposal_id, status=status, reason=reason, now_ms=now_ms)
        self._save()
        return res

    def prune_expired(self, now_ms: int | None = None) -> tuple[SkillProposal, ...]:
        expired = super().prune_expired(now_ms=now_ms)
        if expired:
            self._save()
        return expired
