"""Adapter-neutral Eval records, monoidal RunSummary/RunCoverage, and BenchmarkSnapshot.

Strictly Python stdlib-only. Files are the source of truth; indexes stay rebuildable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from xana.protocol.primitives import TokenUsage
from xana.protocol.versioning import (
    SCHEMA_VERSION,
    assert_reader_compatible,
    canonical_json,
)

EVAL_SCHEMA_VERSION: int = SCHEMA_VERSION

MetricFormat = Literal["percent", "number", "usd", "ms"]
EvalSplit = Literal["train", "dev", "heldout", "live", "regression_pin"]
ExecutionMode = Literal["live", "offline_replay", "shadow"]
TrialStatus = Literal["pass", "fail", "error", "skipped", "running"]
ReadinessStatus = Literal["clean", "dirty", "unknown"]


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    label: str
    format: MetricFormat = "number"
    higher_is_better: bool = True


@dataclass(frozen=True)
class ToolRunCounts:
    total: int = 0
    ok: int = 0
    error: int = 0
    blocked: int = 0
    skipped: int = 0
    timeout: int = 0
    aborted: int = 0
    total_latency_ms: float = 0.0
    by_name: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RunSummary:
    model_calls: int = 0
    stop_reasons: dict[str, int] = field(default_factory=dict)
    model_latency_ms: float = 0.0
    tools: ToolRunCounts = field(default_factory=ToolRunCounts)
    usage: TokenUsage = field(default_factory=TokenUsage)
    errors_by_type: dict[str, int] = field(default_factory=dict)
    step_count: int = 0
    cost_unavailable_reasons: tuple[str, ...] = ()


EMPTY_RUN_SUMMARY = RunSummary()


def _merge_int_dicts(dicts: Sequence[dict[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for d in dicts:
        for k, v in d.items():
            merged[k] = merged.get(k, 0) + v
    return dict(sorted(merged.items()))


def aggregate_run_summaries(summaries: Sequence[RunSummary]) -> RunSummary:
    """Monoidal rollup of N RunSummary objects into the exact same RunSummary shape."""
    if not summaries:
        return EMPTY_RUN_SUMMARY
    usage = TokenUsage()
    for s in summaries:
        usage = usage.add(s.usage)
    tools = ToolRunCounts(
        total=sum(s.tools.total for s in summaries),
        ok=sum(s.tools.ok for s in summaries),
        error=sum(s.tools.error for s in summaries),
        blocked=sum(s.tools.blocked for s in summaries),
        skipped=sum(s.tools.skipped for s in summaries),
        timeout=sum(s.tools.timeout for s in summaries),
        aborted=sum(s.tools.aborted for s in summaries),
        total_latency_ms=sum(s.tools.total_latency_ms for s in summaries),
        by_name=_merge_int_dicts([s.tools.by_name for s in summaries]),
    )
    reasons = tuple(sorted({r for s in summaries for r in s.cost_unavailable_reasons}))
    return RunSummary(
        model_calls=sum(s.model_calls for s in summaries),
        stop_reasons=_merge_int_dicts([s.stop_reasons for s in summaries]),
        model_latency_ms=sum(s.model_latency_ms for s in summaries),
        tools=tools,
        usage=usage,
        errors_by_type=_merge_int_dicts([s.errors_by_type for s in summaries]),
        step_count=sum(s.step_count for s in summaries),
        cost_unavailable_reasons=reasons,
    )


@dataclass(frozen=True)
class RunCoverage:
    tools_available: tuple[str, ...] = ()
    tools_invoked: tuple[str, ...] = ()
    tools_unused: tuple[str, ...] = ()
    models_used: tuple[str, ...] = ()
    providers_used: tuple[str, ...] = ()


EMPTY_RUN_COVERAGE = RunCoverage()


def aggregate_run_coverage(coverages: Sequence[RunCoverage]) -> RunCoverage:
    if not coverages:
        return EMPTY_RUN_COVERAGE
    available = sorted({t for c in coverages for t in c.tools_available})
    invoked = sorted({t for c in coverages for t in c.tools_invoked})
    unused = sorted(set(available) - set(invoked))
    models = sorted({m for c in coverages for m in c.models_used})
    providers = sorted({p for c in coverages for p in c.providers_used})
    return RunCoverage(
        tools_available=tuple(available),
        tools_invoked=tuple(invoked),
        tools_unused=tuple(unused),
        models_used=tuple(models),
        providers_used=tuple(providers),
    )


@dataclass(frozen=True)
class BudgetCaps:
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_wall_ms: int | None = None
    max_steps: int | None = None


@dataclass(frozen=True)
class FailureAttribution:
    fail_class: str = "ok"
    blamed_layers: tuple[str, ...] = ()
    root_cause_span_id: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class EvalTrialRecord:
    trial_id: str
    benchmark: str
    task_id: str
    split: EvalSplit
    status: TrialStatus
    repeat_index: int = 0
    execution_mode: ExecutionMode = "live"
    score: float | None = None
    metrics: dict[str, float | None] = field(default_factory=dict)
    duration_ms: float = 0.0
    run_summary: RunSummary = field(default_factory=RunSummary)
    coverage: RunCoverage = field(default_factory=RunCoverage)
    manifest_hash: str = ""
    tape_path: str | None = None
    budget_caps: BudgetCaps = field(default_factory=BudgetCaps)
    readiness_status: ReadinessStatus = "unknown"
    trajectory_metrics: dict[str, float] = field(default_factory=dict)
    evaluator_reliable: bool = True
    attribution: FailureAttribution = field(default_factory=FailureAttribution)
    schema_version: int = EVAL_SCHEMA_VERSION


@dataclass(frozen=True)
class BenchmarkSnapshot:
    snapshot_id: str
    benchmark: str
    created_at_ms: int
    git_sha: str
    manifest_hash: str
    metric_definitions: tuple[MetricDefinition, ...]
    total: int
    decided: int
    passed: int
    failed: int
    errored: int
    skipped: int
    pass_rate: float | None
    score: float | None
    metrics: dict[str, float | None]
    run_summary: RunSummary
    coverage: RunCoverage
    trials: tuple[EvalTrialRecord, ...]
    schema_version: int = EVAL_SCHEMA_VERSION
    min_reader_version: int = EVAL_SCHEMA_VERSION


def build_benchmark_snapshot(
    *,
    snapshot_id: str,
    benchmark: str,
    created_at_ms: int,
    git_sha: str,
    manifest_hash: str,
    metric_definitions: Sequence[MetricDefinition],
    trials: Sequence[EvalTrialRecord],
) -> BenchmarkSnapshot:
    """Build a BenchmarkSnapshot enforcing the decided-population invariant."""
    trial_tuple = tuple(trials)
    passed = sum(1 for t in trial_tuple if t.status == "pass")
    failed = sum(1 for t in trial_tuple if t.status == "fail")
    errored = sum(1 for t in trial_tuple if t.status == "error")
    skipped = sum(1 for t in trial_tuple if t.status == "skipped")
    decided_trials = [t for t in trial_tuple if t.status in ("pass", "fail")]
    decided = len(decided_trials)

    pass_rate = (passed / decided) if decided > 0 else None
    decided_scores = [t.score for t in decided_trials if t.score is not None]
    mean_score = (sum(decided_scores) / len(decided_scores)) if decided_scores else None

    metric_keys = sorted({m.key for m in metric_definitions} | {k for t in decided_trials for k in t.metrics})
    agg_metrics: dict[str, float | None] = {}
    for key in metric_keys:
        vals = [t.metrics[key] for t in decided_trials if key in t.metrics and t.metrics[key] is not None]
        agg_metrics[key] = (sum(vals) / len(vals)) if vals else None

    return BenchmarkSnapshot(
        snapshot_id=snapshot_id,
        benchmark=benchmark,
        created_at_ms=created_at_ms,
        git_sha=git_sha,
        manifest_hash=manifest_hash,
        metric_definitions=tuple(metric_definitions),
        total=len(trial_tuple),
        decided=decided,
        passed=passed,
        failed=failed,
        errored=errored,
        skipped=skipped,
        pass_rate=pass_rate,
        score=mean_score,
        metrics=agg_metrics,
        run_summary=aggregate_run_summaries([t.run_summary for t in trial_tuple]),
        coverage=aggregate_run_coverage([t.coverage for t in trial_tuple]),
        trials=trial_tuple,
    )


def write_benchmark_snapshot(path: Path, snapshot: BenchmarkSnapshot) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical_json(snapshot) + "\n", encoding="utf-8")


def _parse_token_usage(raw: dict[str, Any]) -> TokenUsage:
    return TokenUsage(
        input_tokens=raw.get("input_tokens", 0),
        output_tokens=raw.get("output_tokens", 0),
        cache_read_tokens=raw.get("cache_read_tokens", 0),
        cache_write_tokens=raw.get("cache_write_tokens", 0),
        reasoning_tokens=raw.get("reasoning_tokens", 0),
        total_tokens=raw.get("total_tokens"),
        cost_usd=raw.get("cost_usd"),
    )


def _parse_run_summary(raw: dict[str, Any]) -> RunSummary:
    t = raw.get("tools", {})
    return RunSummary(
        model_calls=raw.get("model_calls", 0),
        stop_reasons=dict(raw.get("stop_reasons", {})),
        model_latency_ms=raw.get("model_latency_ms", 0.0),
        tools=ToolRunCounts(
            total=t.get("total", 0),
            ok=t.get("ok", 0),
            error=t.get("error", 0),
            blocked=t.get("blocked", 0),
            skipped=t.get("skipped", 0),
            timeout=t.get("timeout", 0),
            aborted=t.get("aborted", 0),
            total_latency_ms=t.get("total_latency_ms", 0.0),
            by_name=dict(t.get("by_name", {})),
        ),
        usage=_parse_token_usage(raw.get("usage", {})),
        errors_by_type=dict(raw.get("errors_by_type", {})),
        step_count=raw.get("step_count", 0),
        cost_unavailable_reasons=tuple(raw.get("cost_unavailable_reasons", ())),
    )


def _parse_run_coverage(raw: dict[str, Any]) -> RunCoverage:
    return RunCoverage(
        tools_available=tuple(raw.get("tools_available", ())),
        tools_invoked=tuple(raw.get("tools_invoked", ())),
        tools_unused=tuple(raw.get("tools_unused", ())),
        models_used=tuple(raw.get("models_used", ())),
        providers_used=tuple(raw.get("providers_used", ())),
    )


def _parse_trial(raw: dict[str, Any]) -> EvalTrialRecord:
    caps = raw.get("budget_caps", {})
    attr = raw.get("attribution", {})
    return EvalTrialRecord(
        trial_id=raw["trial_id"],
        benchmark=raw["benchmark"],
        task_id=raw["task_id"],
        split=raw["split"],
        status=raw["status"],
        repeat_index=raw.get("repeat_index", 0),
        execution_mode=raw.get("execution_mode", "live"),
        score=raw.get("score"),
        metrics=dict(raw.get("metrics", {})),
        duration_ms=raw.get("duration_ms", 0.0),
        run_summary=_parse_run_summary(raw.get("run_summary", {})),
        coverage=_parse_run_coverage(raw.get("coverage", {})),
        manifest_hash=raw.get("manifest_hash", ""),
        tape_path=raw.get("tape_path"),
        budget_caps=BudgetCaps(
            max_tokens=caps.get("max_tokens"),
            max_cost_usd=caps.get("max_cost_usd"),
            max_wall_ms=caps.get("max_wall_ms"),
            max_steps=caps.get("max_steps"),
        ),
        readiness_status=raw.get("readiness_status", "unknown"),
        trajectory_metrics=dict(raw.get("trajectory_metrics", {})),
        evaluator_reliable=raw.get("evaluator_reliable", True),
        attribution=FailureAttribution(
            fail_class=attr.get("fail_class", "ok"),
            blamed_layers=tuple(attr.get("blamed_layers", ())),
            root_cause_span_id=attr.get("root_cause_span_id"),
            notes=attr.get("notes", ""),
        ),
        schema_version=raw.get("schema_version", EVAL_SCHEMA_VERSION),
    )


def read_benchmark_snapshot(path: Path) -> BenchmarkSnapshot:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    assert_reader_compatible(raw.get("min_reader_version", 1), supported=EVAL_SCHEMA_VERSION)
    return BenchmarkSnapshot(
        snapshot_id=raw["snapshot_id"],
        benchmark=raw["benchmark"],
        created_at_ms=raw["created_at_ms"],
        git_sha=raw["git_sha"],
        manifest_hash=raw["manifest_hash"],
        metric_definitions=tuple(
            MetricDefinition(
                key=m["key"],
                label=m["label"],
                format=m.get("format", "number"),
                higher_is_better=m.get("higher_is_better", True),
            )
            for m in raw.get("metric_definitions", ())
        ),
        total=raw["total"],
        decided=raw["decided"],
        passed=raw["passed"],
        failed=raw["failed"],
        errored=raw["errored"],
        skipped=raw["skipped"],
        pass_rate=raw.get("pass_rate"),
        score=raw.get("score"),
        metrics=dict(raw.get("metrics", {})),
        run_summary=_parse_run_summary(raw.get("run_summary", {})),
        coverage=_parse_run_coverage(raw.get("coverage", {})),
        trials=tuple(_parse_trial(t) for t in raw.get("trials", ())),
        schema_version=raw.get("schema_version", EVAL_SCHEMA_VERSION),
        min_reader_version=raw.get("min_reader_version", EVAL_SCHEMA_VERSION),
    )
