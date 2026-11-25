"""Standalone CLI for xana.evolution: `python -m xana.evolution bench [--json]`.

Demonstrates a complete single-module RSI eval + anti-overfitting heldout
promotion/rollback loop and multi-version assembly manifest without host/boot.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from xana.evolution.contracts import LedgerEntry, PromotionCriteria, VersionAssembly
from xana.evolution.evals import batch_to_benchmark_snapshot
from xana.evolution.ledger import Ledger
from xana.evolution.promotion import StatisticalPromotionGate
from xana.evolution.tape_recorder import EvolutionTapeRecorder
from xana.protocol.eval import (
    EvalTrialRecord,
    MetricDefinition,
    RunSummary,
    build_benchmark_snapshot,
)
from xana.protocol.primitives import ComponentVersion
from xana.protocol.tape import RunManifest, TapeHeader, TapeWriter, read_tape
from xana.protocol.versioning import to_wire


async def _run_bench():
    trials: list[EvalTrialRecord] = []

    def _add(task_id: str, ok: bool, duration_ms: float) -> None:
        trials.append(
            EvalTrialRecord(
                trial_id=f"trial-{task_id}",
                benchmark="evolution_subsystem_bench",
                task_id=task_id,
                split="train",
                status="pass" if ok else "fail",
                score=1.0 if ok else 0.0,
                duration_ms=round(duration_ms, 2),
                run_summary=RunSummary(step_count=1),
            )
        )

    with tempfile.TemporaryDirectory(prefix="xana_evolution_bench_") as tmp:
        root = Path(tmp)
        gate = StatisticalPromotionGate()
        ledger = Ledger(home=root / "evolution")

        # 1. Single-module RSI promotion (pass) & anti-overfitting rollback (fail on heldout / keep veto)
        t0 = time.monotonic()
        base = [
            {"case_id": "t1", "rep": 0, "status": "scored", "score": 0.4},
            {"case_id": "t2", "rep": 0, "status": "scored", "score": 0.5},
            {"case_id": "k1", "rep": 0, "status": "scored", "score": 0.9},
            {"case_id": "h1", "rep": 0, "status": "scored", "score": 0.7},
        ]
        cand_good = [
            {"case_id": "t1", "rep": 0, "status": "scored", "score": 0.8},
            {"case_id": "t2", "rep": 0, "status": "scored", "score": 0.85},
            {"case_id": "k1", "rep": 0, "status": "scored", "score": 0.9},
            {"case_id": "h1", "rep": 0, "status": "scored", "score": 0.75},
        ]
        cand_overfit = [
            {"case_id": "t1", "rep": 0, "status": "scored", "score": 0.9},
            {"case_id": "t2", "rep": 0, "status": "scored", "score": 0.9},
            {"case_id": "k1", "rep": 0, "status": "scored", "score": 0.9},
            {"case_id": "h1", "rep": 0, "status": "scored", "score": 0.5},  # heldout regresses!
        ]
        cand_keep_veto = [
            {"case_id": "t1", "rep": 0, "status": "scored", "score": 0.9},
            {"case_id": "t2", "rep": 0, "status": "scored", "score": 0.9},
            {"case_id": "k1", "rep": 0, "status": "scored", "score": 0.6},  # keep regresses!
            {"case_id": "h1", "rep": 0, "status": "scored", "score": 0.75},
        ]
        criteria = PromotionCriteria(
            target_cases=("t1", "t2"),
            keep_cases=("k1",),
            heldout_cases=("h1",),
            min_gain=0.1,
            gate=0.05,
        )
        d_good = gate.evaluate(base, cand_good, criteria=criteria)
        d_overfit = gate.evaluate(base, cand_overfit, criteria=criteria)
        d_veto = gate.evaluate(base, cand_keep_veto, criteria=criteria)
        ok1 = d_good.verdict == "pass" and d_overfit.verdict == "fail" and d_veto.verdict == "fail"
        _add("promotion_gate_pass_and_rollback", ok1, (time.monotonic() - t0) * 1000.0)

        # 2. Multi-version assembly + Stage 0 BenchmarkSnapshot + append-only Ledger + Tape round-trip
        t1 = time.monotonic()
        now_ms = int(time.time() * 1000)
        components = (
            ComponentVersion(name="memory.retrieval.sag2", kind="memory_retrieval_policy", version="1.0.0"),
            ComponentVersion(name="harness.compaction.default", kind="compaction_policy", version="1.0.0"),
            gate.component_version(),
        )
        manifest = RunManifest(
            tape_id="t-evo",
            run_id="r-evo",
            session_id="s-evo",
            created_at_ms=now_ms,
            model="kimi-for-coding",
            provider="kimi",
            components=components,
        )
        mhash = manifest.manifest_hash()
        assembly = VersionAssembly(
            assembly_id="asm-v1",
            components=tuple((c.name, c.kind, c.version) for c in components),
            manifest_hash=mhash,
        )
        snap_cand = batch_to_benchmark_snapshot(
            suite_name="rsi_demo_suite",
            batch_id="b-cand-1",
            per_case=cand_good,
            manifest_hash=mhash,
        )
        entry_kept = LedgerEntry(
            episode_id="ep-001",
            goal="improve memory retrieval recall",
            started_at=time.time(),
            verdict="kept",
            delta_vs_base=d_good.target_mean_diff,
            agent="demo",
            manifest_hash=mhash,
            snapshot_id=snap_cand.snapshot_id,
        )
        entry_rolled = LedgerEntry(
            episode_id="ep-002",
            goal="reject overfit candidate",
            started_at=time.time(),
            verdict="rolled_back",
            delta_vs_base=d_overfit.target_mean_diff,
            agent="demo",
            manifest_hash=mhash,
        )
        await ledger.append(entry_kept)
        await ledger.append(entry_rolled)
        bundle = root / "tape"
        header = TapeHeader(tape_id="t-evo", session_id="s-evo", run_id="r-evo", created_at_ms=now_ms)
        with TapeWriter(bundle, header, manifest=manifest) as writer:
            rec = EvolutionTapeRecorder(writer)
            rec.record_promotion(episode_id="ep-001", decision=d_good, manifest_hash=mhash, policy=gate.component_version())
            rec.record_ledger(entry_kept)
            rec.record_promotion(episode_id="ep-002", decision=d_overfit, manifest_hash=mhash, policy=gate.component_version())
            rec.record_ledger(entry_rolled)
            writer.close(status="completed")

        tape = read_tape(bundle)
        read_entries = await ledger.read_all()
        violations = ledger.lint()
        ok2 = (
            len(assembly.components) == 3
            and snap_cand.passed == 4
            and len(read_entries) == 2
            and read_entries[0].manifest_hash == mhash
            and violations == []
            and not hasattr(ledger, "delete")
            and tape.status == "complete"
            and {"eval.promotion", "eval.ledger"}.issubset({e.type for e in tape.events})
        )
        _add("assembly_snapshot_ledger_and_tape", ok2, (time.monotonic() - t1) * 1000.0)

    return build_benchmark_snapshot(
        snapshot_id=f"evolution-bench-{int(time.time())}",
        benchmark="evolution_subsystem_bench",
        created_at_ms=int(time.time() * 1000),
        git_sha="local",
        manifest_hash="",
        metric_definitions=(MetricDefinition(key="pass_rate", label="Pass Rate", format="percent"),),
        trials=tuple(trials),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xana.evolution")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bench_p = sub.add_parser("bench", help="Run standalone Evolution & Eval RSI micro-benchmark")
    bench_p.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON snapshot")
    args = parser.parse_args(argv)
    if args.cmd == "bench":
        snap = asyncio.run(_run_bench())
        if args.as_json:
            print(json.dumps(to_wire(snap), ensure_ascii=False, indent=2))
        else:
            rate = snap.passed / snap.total if snap.total else 0.0
            print(f"[xana.evolution bench] {snap.passed}/{snap.total} passed (pass_rate={rate:.2f})")
            for t in snap.trials:
                print(f"  - {t.task_id}: {t.status.upper()} ({t.duration_ms:.2f}ms)")
        return 0 if snap.failed == 0 and snap.errored == 0 else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
