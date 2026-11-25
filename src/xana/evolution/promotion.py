"""Default statistical anti-overfitting PromotionGate implementation."""
from __future__ import annotations

from typing import Any

from xana.evolution.contracts import PromotionCriteria, PromotionDecision
from xana.evolution.evals import verdict as _paired_verdict
from xana.evolution.interfaces import PromotionGate
from xana.protocol.diag import DiagLogger
from xana.protocol.primitives import ComponentVersion

_diag = DiagLogger("eval")


class StatisticalPromotionGate(PromotionGate):
    """Paired train/keep/heldout statistical gate wrapping `evolution.evals.verdict`."""

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="evolution.promotion.statistical",
            kind="promotion_gate",
            version="1.0.0",
            policy_version="paired-heldout-v1",
        )

    def evaluate(
        self,
        base_per_case: list[dict[str, Any]],
        cand_per_case: list[dict[str, Any]],
        *,
        criteria: PromotionCriteria,
    ) -> PromotionDecision:
        raw = _paired_verdict(
            base_per_case,
            cand_per_case,
            target=list(criteria.target_cases),
            keep=list(criteria.keep_cases),
            min_gain=criteria.min_gain,
            heldout=set(criteria.heldout_cases),
            gate=criteria.gate,
        )
        reasons = tuple(r for r in (raw.get("reasons") or ()) if r)
        warnings = tuple(w for w in (raw.get("warnings") or ()) if w)
        target_info = raw.get("target") or {}
        keep_info = raw.get("keep") or {}
        heldout_info = raw.get("heldout") or {}
        decision = PromotionDecision(
            verdict=raw["verdict"],
            reasons=reasons,
            warnings=warnings,
            target_mean_diff=float(target_info.get("mean_diff") or 0.0),
            heldout_mean_diff=heldout_info.get("mean_diff"),
            keep_worse=tuple(keep_info.get("worse") or ()),
            target_worse=tuple(target_info.get("worse") or ()),
        )
        _diag.op(
            "promotion.evaluate",
            verdict=decision.verdict,
            target_mean_diff=decision.target_mean_diff,
            heldout_mean_diff=decision.heldout_mean_diff,
            reasons=len(reasons),
            warnings=len(warnings),
        )
        if decision.warnings:
            _diag.anomaly("promotion.leak_warning", warnings=list(decision.warnings))
        return decision
