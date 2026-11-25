"""Deterministic offline test doubles for the Memory subsystem."""
from __future__ import annotations

from xana.memory.contracts import (
    EvidenceCandidate,
    EvidenceJudgment,
    EvidenceScore,
)
from xana.memory.store import search_terms


class FakeEvidenceJudge:
    """Offline deterministic EvidenceJudge using normalized lexical overlap.

    Suitable for unit tests and `python -m xana.memory bench` without a live
    Karpo / JEV server.
    """

    def __init__(self, *, model: str = "fake-lexical-judge") -> None:
        self._model = model
        self.calls: int = 0

    async def __call__(
        self,
        query: str,
        candidates: tuple[EvidenceCandidate, ...],
        evidence: tuple[EvidenceCandidate, ...],
    ) -> EvidenceJudgment:
        self.calls += 1
        q_terms = set(search_terms(query))
        scores: list[EvidenceScore] = []
        for cand in candidates:
            c_terms = set(search_terms(f"{cand.title} {cand.content}"))
            if not q_terms or not c_terms:
                score = 0.0
            else:
                overlap = len(q_terms & c_terms)
                score = min(1.0, overlap / max(1, len(q_terms)))
            scores.append(EvidenceScore(candidate_id=cand.id, score=round(score, 4)))
        return EvidenceJudgment(
            scores=tuple(scores),
            model=self._model,
            input_tokens=sum(len(c.content) // 4 for c in candidates),
            output_tokens=len(candidates) * 2,
            requests=1,
        )
