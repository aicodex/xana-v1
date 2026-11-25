"""Standalone CLI for xana.tools: `python -m xana.tools bench`."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.protocol.eval import EvalTrialRecord, MetricDefinition, RunSummary, build_benchmark_snapshot
from xana.protocol.tape import RunManifest, TapeHeader, TapeWriter, read_tape
from xana.protocol.versioning import to_wire
from xana.tools.batches import Done, partition, run_batch
from xana.tools.contracts import DisclosureContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.disclosure import DefaultToolDisclosurePolicy
from xana.tools.observation import DefaultObservationPolicy
from xana.tools.tape_recorder import ToolTapeRecorder


def _spec(name: str, *, defer: bool = False, open_world: bool = False, max_chars: int = 50_000, yields: bool = False) -> ToolSpec:
    return ToolSpec(
        component=ComponentSpec(
            kind="tool",
            name=name,
            qualified_name=f"bench.{name}",
            description=f"{name} tool",
            source=ComponentSource(source_type="builtin", scope="system"),
        ),
        input_schema={"type": "object"},
        defer=defer,
        open_world=open_world,
        max_result_chars=max_chars,
        yields_to_native=yields,
    )


async def _cmd_bench() -> int:
    trials: list[EvalTrialRecord] = []

    def _add(task_id: str, ok: bool, duration_ms: float) -> None:
        trials.append(EvalTrialRecord(
            trial_id=f"trial-{task_id}",
            benchmark="tools_pipeline_bench",
            task_id=task_id,
            split="train",
            status="pass" if ok else "fail",
            score=1.0 if ok else 0.0,
            duration_ms=round(duration_ms, 2),
            run_summary=RunSummary(step_count=1),
        ))

    # 1. Parallel batch ordering & partitioning
    t0 = time.monotonic()
    parts = partition([True, True, False, True, True])
    calls = [{"i": 0, "delay": 0.04}, {"i": 1, "delay": 0.01}]

    async def _disp(c, emit):
        await asyncio.sleep(c["delay"])
        return ToolResult(ok=True, content=c["i"]), f"out-{c['i']}"

    done_indices = [ev.index async for ev in run_batch(calls, _disp) if isinstance(ev, Done)]
    _add("parallel_batch_ordering", parts == [[0, 1], [2], [3, 4]] and done_indices == [0, 1], (time.monotonic() - t0) * 1000.0)

    # 2. Central observation truncation & open_world taint
    t0 = time.monotonic()
    obs_pol = DefaultObservationPolicy()
    inv = ToolInvocation("web_fetch", {"url": "https://x"}, "c1", "s1", "r1")
    sp = _spec("web_fetch", open_world=True, max_chars=200)
    obs = obs_pol.normalize(inv, sp, ToolResult(ok=True, content="x"), rendered="A" * 500)
    _add(
        "observation_truncation_and_taint",
        obs.truncated and obs.omitted_chars == 300 and obs.taint == "external_untrusted" and len(obs.model_text) < 250,
        (time.monotonic() - t0) * 1000.0,
    )

    # 3. Deferred tool disclosure & native web search yield
    t0 = time.monotonic()
    disc = DefaultToolDisclosurePolicy()
    specs = (_spec("tool_search"), _spec("read"), _spec("heavy_tool", defer=True), _spec("web_search", yields=True))
    plan = disc.plan(specs, DisclosureContext(provider="deepseek-v4", available_tools=None, revealed_tools=()))
    names = {s["name"] for s in plan.schemas}
    def_names = {d[0] for d in plan.deferred}
    _add(
        "deferred_disclosure_and_native_yield",
        names == {"tool_search", "read"} and def_names == {"heavy_tool"} and plan.web_search_native,
        (time.monotonic() - t0) * 1000.0,
    )

    # 4. Tape recording with >64KB CAS blob offload
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as td:
        bundle = Path(td) / "tape"
        manifest = RunManifest(
            tape_id="t-tools",
            run_id="r-tools",
            session_id="s-tools",
            created_at_ms=int(time.time() * 1000),
            model="m",
            provider="p",
            components=(obs_pol.component_version(), disc.component_version()),
        )
        header = TapeHeader(tape_id="t-tools", session_id="s-tools", run_id="r-tools", created_at_ms=manifest.created_at_ms)
        with TapeWriter(bundle, header, manifest=manifest) as writer:
            rec = ToolTapeRecorder(writer)
            big_obs = obs_pol.normalize(inv, _spec("big", max_chars=100_000), ToolResult(ok=True, content="big"), rendered="B" * 70_000)
            rec.record_call(invocation=inv, result=ToolResult(ok=True, content="big"), observation=big_obs, elapsed_ms=12.5, obs_policy=obs_pol.component_version())
            writer.close(status="completed")
        tape = read_tape(bundle)
        blobs = list((bundle / "blobs").iterdir())
        _add("tool_tape_cas_blob", tape.status == "complete" and len(tape.events) == 1 and len(blobs) >= 1, (time.monotonic() - t0) * 1000.0)

    snap = build_benchmark_snapshot(
        snapshot_id=f"tools-bench-{int(time.time())}",
        benchmark="tools_pipeline_bench",
        created_at_ms=int(time.time() * 1000),
        git_sha="local",
        manifest_hash="",
        metric_definitions=(MetricDefinition(key="pass_rate", label="Pass Rate", format="percent"),),
        trials=tuple(trials),
    )
    print(json.dumps(to_wire(snap), ensure_ascii=False, indent=2))
    return 0 if snap.failed == 0 and snap.errored == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xana.tools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bench", help="run tools pipeline, observation & disclosure benchmark")
    args = parser.parse_args(argv)
    if args.cmd == "bench":
        return asyncio.run(_cmd_bench())
    return 1


if __name__ == "__main__":
    sys.exit(main())
