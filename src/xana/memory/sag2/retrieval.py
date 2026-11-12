"""Adapted from Zleap SAG2 bb7f8275 (MIT); see NOTICE.md for changes."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from .index import CorpusIndex
from .text import rank_dpp

EvidenceScorer = Callable[..., Awaitable[tuple[list[dict], dict]]]


async def search_sag2(
    index: CorpusIndex,
    scorer: EvidenceScorer,
    query: str,
    *,
    candidate_k: int = 100,
    seed_k: int = 10,
    max_hops: int = 2,
    expansion_term_limit: int = 20,
    context: dict | None = None,
) -> dict:
    if not 0 <= max_hops <= 2:
        raise ValueError("SAG2 supports at most two expansion rounds")
    started = time.perf_counter()
    retained, carried, protected, latest = {}, {}, {}, {}
    expanded, seen = set(), set()
    frontier, trace, baseline = [], [], []
    usage = {
        "requests": 0,
        "judgments": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "batches": [],
    }

    def ranked() -> list[dict]:
        return sorted(
            retained.values(),
            key=lambda c: (-c["jev_score"], -c["fusion_score"], c["id"]),
        )

    expression = index.match_query(query)
    for hop in range(max_hops + 1):
        if not expression or (hop and not frontier):
            break
        terms = (
            index.expansion_terms(query, frontier, expansion_term_limit) if hop else []
        )
        if hop and not terms:
            break
        hits, queries = (
            index.retrieve_expansion(expression, terms, seen, candidate_k)
            if hop
            else (index.retrieve(expression, seen, candidate_k), [])
        )
        candidates = [
            {**hit, "hop": hop, "fusion_score": 1 / (61 + i)}
            for i, hit in enumerate(hits)
        ]
        trace.append(
            {
                "stage": "expansion" if hop else "bm25",
                "hop": hop,
                "count": len(candidates),
                "candidate_document_ids": [c["document_id"] for c in candidates],
                "candidate_ids": [c["id"] for c in candidates],
                "terms": [term["term"] for term in terms],
                "term_scores": terms,
                "expansion_queries": queries,
            }
        )
        if not candidates:
            break
        if hop == 0:
            baseline = candidates
            protected = {c["id"]: c for c in candidates[:5]}
        carried.update({c["id"]: c for c in frontier})
        pool = (
            list(
                {
                    c["id"]: c
                    for c in [*protected.values(), *carried.values(), *candidates]
                }.values()
            )
            if hop
            else candidates
        )
        evidence = (
            list(
                {c["id"]: c for c in [*carried.values(), *candidates[:seed_k]]}.values()
            )
            if hop
            else []
        )
        if hop == 2:
            carried_candidates = [c for c in pool if c["id"] in latest]
            incoming = [c for c in pool if c["id"] not in latest][:20]
            previous = sorted(latest.values(), key=lambda c: (-c["jev_score"], c["id"]))
            history = list(
                {c["id"]: c for c in [*carried_candidates, *previous]}.values()
            )
            pool = history[: len(pool) - len(incoming)] + incoming
            evidence = [
                {**c, "previous_jev_score": c["jev_score"]} for c in ranked()[:10]
            ]
        score_options = {"context": context} if context is not None else {}
        scored, round_usage = await scorer(query, pool, evidence, **score_options)
        latest.update({c["id"]: c for c in scored})
        for key in ("requests", "judgments", "input_tokens", "output_tokens"):
            usage[key] += round_usage[key]
        usage["batches"].extend(
            {"hop": hop, **batch} for batch in round_usage["batches"]
        )
        retained = {c["id"]: c for c in scored}
        seen.update(retained)
        trace.append(
            {
                "stage": "jev",
                "hop": hop,
                "count": len(scored),
                "carried_seed_count": len(carried),
                "protected_bm25_count": len(protected),
                "evidence_ids": [c["id"] for c in evidence],
                "scores": [
                    {
                        "id": c["id"],
                        "score": c["jev_score"],
                    }
                    for c in ranked()
                ],
                "batches": round_usage["batches"],
            }
        )
        frontier = [c for c in ranked() if c["id"] not in expanded][:seed_k]
        expanded.update(c["id"] for c in frontier)
    final = ranked()[:candidate_k]
    final = rank_dpp(final[:10], index.diversity_idf) + final[10:]
    return {
        "candidates": final,
        "baseline": baseline,
        "trace": trace,
        "jev_usage": usage,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "algorithm": "SAG2",
    }
