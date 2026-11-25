"""Standalone CLI for xana.skills: `python -m xana.skills bench`."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from xana.protocol.eval import EvalTrialRecord, MetricDefinition, RunSummary, build_benchmark_snapshot
from xana.protocol.tape import RunManifest, TapeHeader, TapeWriter, read_tape
from xana.protocol.versioning import to_wire
from xana.skills.bundled.coding import CodingSkill
from xana.skills.bundled.loop import LoopSkill
from xana.skills.catalog import DefaultSkillCatalogPolicy
from xana.skills.contracts import CatalogContext, SkillActivation, SkillProposal
from xana.skills.proposals import FileSkillProposalQueue, InMemorySkillProposalQueue
from xana.skills.registry import InMemorySkillRegistry
from xana.skills.tape_recorder import SkillTapeRecorder


async def _cmd_bench() -> int:
    trials: list[EvalTrialRecord] = []

    def _add(task_id: str, ok: bool, duration_ms: float) -> None:
        trials.append(EvalTrialRecord(
            trial_id=f"trial-{task_id}",
            benchmark="skills_subsystem_bench",
            task_id=task_id,
            split="train",
            status="pass" if ok else "fail",
            score=1.0 if ok else 0.0,
            duration_ms=round(duration_ms, 2),
            run_summary=RunSummary(step_count=1),
        ))

    # 1. Discovery & catalog rendering + hash stability
    t0 = time.monotonic()
    reg = InMemorySkillRegistry()
    await reg.register(CodingSkill())
    await reg.register(LoopSkill())
    discovered = await reg.discover(SkillActivation(kind="system", session_id="s1", agent_name="chat"))
    specs = tuple(s.spec() for s in discovered)
    pol = DefaultSkillCatalogPolicy()
    ctx = CatalogContext(session_id="s1", agent_name="chat")
    p1 = pol.plan(specs, ctx)
    p2 = pol.plan(specs, ctx)
    _add(
        "catalog_render_and_hash_stability",
        len(p1.entries) == 2 and p1.catalog_hash == p2.catalog_hash and "# 可用技能" in p1.rendered,
        (time.monotonic() - t0) * 1000.0,
    )

    # 2. Proposal queue conformance (InMemory + File)
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as td:
        ok_q = True
        for q in (InMemorySkillProposalQueue(), FileSkillProposalQueue(Path(td) / "proposals.json")):
            p = q.submit(SkillProposal("p1", "new-skill", "desc", "body", created_at_ms=1000, expires_at_ms=2000))
            assert p.status == "pending"
            q.submit(SkillProposal("p2", "exp-skill", "desc", "body", created_at_ms=1000, expires_at_ms=1500))
            exp = q.prune_expired(now_ms=1600)
            dec = q.decide("p1", status="approved", reason="useful", now_ms=1700)
            ok_q &= len(exp) == 1 and exp[0].proposal_id == "p2" and dec.status == "approved"
        _add("proposal_queue_conformance", ok_q, (time.monotonic() - t0) * 1000.0)

    # 3. Tape recording round-trip
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as td:
        bundle = Path(td) / "tape"
        manifest = RunManifest(
            tape_id="t-sk", run_id="r-sk", session_id="s-sk", created_at_ms=int(time.time() * 1000),
            model="m", provider="p", components=(pol.component_version(),),
        )
        header = TapeHeader(tape_id="t-sk", session_id="s-sk", run_id="r-sk", created_at_ms=manifest.created_at_ms)
        with TapeWriter(bundle, header, manifest=manifest) as writer:
            rec = SkillTapeRecorder(writer)
            rec.record_catalog(ctx, p1, policy=pol.component_version())
            rec.record_load(session_id="s-sk", agent_name="chat", run_id="r-sk", skill_name="coding", ok=True, latency_ms=1.2, chars=500)
            writer.close(status="completed")
        tape = read_tape(bundle)
        _add("skill_tape_roundtrip", tape.status == "complete" and [e.type for e in tape.events] == ["skill.catalog", "skill.load"], (time.monotonic() - t0) * 1000.0)

    snap = build_benchmark_snapshot(
        snapshot_id=f"skills-bench-{int(time.time())}",
        benchmark="skills_subsystem_bench",
        created_at_ms=int(time.time() * 1000),
        git_sha="local",
        manifest_hash="",
        metric_definitions=(MetricDefinition(key="pass_rate", label="Pass Rate", format="percent"),),
        trials=tuple(trials),
    )
    print(json.dumps(to_wire(snap), ensure_ascii=False, indent=2))
    return 0 if snap.failed == 0 and snap.errored == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xana.skills")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bench", help="run standalone skills catalog, proposal & tape benchmark")
    args = parser.parse_args(argv)
    if args.cmd == "bench":
        return asyncio.run(_cmd_bench())
    return 1


if __name__ == "__main__":
    sys.exit(main())
