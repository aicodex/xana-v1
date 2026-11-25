"""Standalone runnable entrypoint for the Memory & SAG2 subsystem.

Usage:
    python -m xana.memory bench [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from xana.memory.contracts import RetrievalContext
from xana.memory.project_search import ProjectMemorySearch
from xana.memory.recall import DefaultMemoryRetrievalPolicy, ProjectMemoryInjector
from xana.memory.store import ProjectMemoryStore
from xana.memory.tape_recorder import MemoryTapeRecorder
from xana.memory.testing import FakeEvidenceJudge
from xana.protocol.eval import (
    EvalTrialRecord,
    MetricDefinition,
    RunSummary,
    build_benchmark_snapshot,
)
from xana.protocol.tape import RunManifest, TapeHeader, TapeWriter, read_tape
from xana.protocol.versioning import to_wire


async def _run_bench():
    trials: list[EvalTrialRecord] = []

    def _add(task_id: str, ok: bool, duration_ms: float) -> None:
        trials.append(
            EvalTrialRecord(
                trial_id=f"trial-{task_id}",
                benchmark="memory_subsystem_bench",
                task_id=task_id,
                split="train",
                status="pass" if ok else "fail",
                score=1.0 if ok else 0.0,
                duration_ms=round(duration_ms, 2),
                run_summary=RunSummary(step_count=1),
            )
        )

    with tempfile.TemporaryDirectory(prefix="xana_memory_bench_") as tmp:
        root = Path(tmp)
        bundle = root / "tape"
        now_ms = int(time.time() * 1000)
        store = ProjectMemoryStore(root / "proj_a")
        policy = DefaultMemoryRetrievalPolicy(store)
        manifest = RunManifest(
            tape_id="t-mem",
            run_id="r-mem",
            session_id="s-mem",
            created_at_ms=now_ms,
            model="fake-lexical-judge",
            provider="local",
            components=(policy.component_version(),),
        )
        header = TapeHeader(
            tape_id="t-mem",
            session_id="s-mem",
            run_id="r-mem",
            created_at_ms=now_ms,
        )
        with TapeWriter(bundle, header, manifest=manifest) as writer:
            recorder = MemoryTapeRecorder(writer)

            # Case 1: Store CRUD + snapshot + rollback + fork isolation
            t0 = time.monotonic()
            store.write(
                "pref_style",
                name="回答风格偏好",
                description="用户希望简洁、给出可执行命令",
                mtype="user",
                body="保持回复简短，先给结论和命令。",
                pinned=True,
            )
            store.write(
                "karpo_baseline",
                name="Karpo 4B SAG2 记忆基线",
                description="本地 4B Karpo 决策模型运行在 18440 端口作为 SAG2 评分器",
                mtype="project",
                body="记忆召回使用 SAG2 算法配合本地 karpo-decision-4B，端口 18440，延迟约 300ms。",
            )
            snap = store.snapshot()
            recorder.record_snapshot(snap, action="snapshot")
            store.write(
                "temp_note",
                name="临时记录",
                description="应当被回滚清除的记录",
                mtype="project",
                body="不应该保留。",
            )
            forked = store.fork(root / "proj_fork")
            store.rollback(snap)
            recorder.record_snapshot(snap, action="rollback")
            ok1 = (
                len(store.list()) == 2
                and store.get("temp_note") is None
                and forked.get("temp_note") is not None
            )
            _add("store_snapshot_rollback_fork", ok1, (time.monotonic() - t0) * 1000.0)

            # Case 2: SAG2 + FakeEvidenceJudge + DefaultMemoryRetrievalPolicy + Tape
            t1 = time.monotonic()
            judge = FakeEvidenceJudge()
            search = ProjectMemorySearch(
                project_root=root / "proj_a",
                db_path=root / "sag2.sqlite",
                judge=judge,
                timeout_s=10.0,
                tape_recorder=recorder,
            )
            injector = ProjectMemoryInjector(
                store,
                mode="sag2",
                search_engine=search,
                tape_recorder=recorder,
            )
            blocks = await injector.select(
                session_id="bench-s1",
                agent_name="bench-agent",
                user_input="Karpo 4B SAG2 端口是多少",
                cwd=str(root / "proj_a"),
            )
            placements = [b.placement for b in blocks]
            ok2 = (
                "index" in placements
                and "pinned" in placements
                and "recall" in placements
                and any("karpo_baseline.md" in b.node_path for b in blocks)
                and judge.calls >= 1
            )
            _add("sag2_recall_and_tape", ok2, (time.monotonic() - t1) * 1000.0)

            # Case 3: Short-query skip + tape event verification
            t2 = time.monotonic()
            policy_with_search = DefaultMemoryRetrievalPolicy(store, search_engine=search)
            plan = await policy_with_search.plan(
                RetrievalContext(
                    session_id="bench-s1",
                    agent_name="bench-agent",
                    user_input="好",
                    cwd=str(root / "proj_a"),
                    records=store.list(),
                )
            )
            writer.close(status="completed")

        tape = read_tape(bundle)
        event_types = {e.type for e in tape.events}
        ok3 = (
            tape.status == "complete"
            and plan.skipped_reason == "below_min_terms"
            and not plan.recalled_paths
            and {"memory.snapshot", "memory.search", "memory.recall"}.issubset(event_types)
        )
        _add("short_query_skip_and_tape_kinds", ok3, (time.monotonic() - t2) * 1000.0)

    return build_benchmark_snapshot(
        snapshot_id=f"memory-bench-{int(time.time())}",
        benchmark="memory_subsystem_bench",
        created_at_ms=int(time.time() * 1000),
        git_sha="local",
        manifest_hash="",
        metric_definitions=(MetricDefinition(key="pass_rate", label="Pass Rate", format="percent"),),
        trials=tuple(trials),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xana.memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bench_p = sub.add_parser("bench", help="Run standalone Memory & SAG2 micro-benchmark")
    bench_p.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON snapshot")
    args = parser.parse_args(argv)

    if args.cmd == "bench":
        snap = asyncio.run(_run_bench())
        if args.as_json:
            print(json.dumps(to_wire(snap), ensure_ascii=False, indent=2))
        else:
            rate = snap.passed / snap.total if snap.total else 0.0
            print(
                f"[xana.memory bench] {snap.passed}/{snap.total} passed "
                f"(pass_rate={rate:.2f})"
            )
            for t in snap.trials:
                print(f"  - {t.task_id}: {t.status.upper()} ({t.duration_ms:.2f}ms)")
        return 0 if snap.failed == 0 and snap.errored == 0 else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
