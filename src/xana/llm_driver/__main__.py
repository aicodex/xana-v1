"""Standalone CLI entry point for xana.llm_driver: `python -m xana.llm_driver <bench|replay|smoke>`."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from xana.llm_driver.auth import KimiOAuthResolver, KimiTokenStore, StaticApiKeyResolver, api_base, credentials_path
from xana.llm_driver.contracts import ModelMessage, ModelRequest, ModelUsage
from xana.llm_driver.driver import build_driver, complete
from xana.llm_driver.policies import DefaultRetryPolicy, PolicyBundle
from xana.llm_driver.profiles import KIMI_PROFILE, OPENAI_COMPATIBLE_PROFILE, make_profile
from xana.llm_driver.tape_recorder import LlmTapeRecorder
from xana.llm_driver.testing import FakeLLMServer, FaultStep, ReplayClient, ScriptedTurn
from xana.protocol.eval import (
    EvalTrialRecord,
    MetricDefinition,
    RunCoverage,
    RunSummary,
    build_benchmark_snapshot,
)
from xana.protocol.primitives import ComponentVersion
from xana.protocol.tape import RunManifest, TapeHeader, TapeWriter
from xana.protocol.versioning import to_wire


async def _cmd_bench() -> int:
    """Run deterministic local loopback SSE + fault-injection benchmark and emit BenchmarkSnapshot."""
    async def _nosleep(_: float) -> None:
        return None

    trials: list[EvalTrialRecord] = []
    policies = PolicyBundle(
        retry=DefaultRetryPolicy(max_retries=3, base_delay_s=0.0, jitter_ratio=0.0),
        stream_recovery=PolicyBundle.default().stream_recovery,
        rewrite=PolicyBundle.default().rewrite,
        routing=PolicyBundle.default().routing,
    )
    scenarios = [
        ("normal_stream", [ScriptedTurn(text_deltas=("ok",), usage=ModelUsage(input_tokens=10, output_tokens=2))]),
        ("retry_429", [
            FaultStep(kind="http_error", status=429, body="rate limit", headers={"Retry-After": "0"}),
            ScriptedTurn(text_deltas=("recovered",), usage=ModelUsage(input_tokens=10, output_tokens=3)),
        ]),
        ("mid_stream_eof", [
            FaultStep(kind="drop_mid_stream", partial_text="half"),
            ScriptedTurn(text_deltas=("complete",), usage=ModelUsage(input_tokens=10, output_tokens=4)),
        ]),
    ]
    for name, steps in scenarios:
        with FakeLLMServer(steps) as srv:
            profile = make_profile(OPENAI_COMPATIBLE_PROFILE, base_url=srv.base_url, trust_env=False)
            driver = build_driver(profile, credentials=StaticApiKeyResolver("sk"), policies=policies, sleep=_nosleep)
            try:
                req = ModelRequest(provider="openai_compatible", model="bench-m", messages=(ModelMessage("user", name),))
                out = await complete(driver, req)
            finally:
                await driver.aclose()
        trials.append(EvalTrialRecord(
            trial_id=f"trial-{name}",
            benchmark="llm_driver_fault_bench",
            task_id=name,
            split="train",
            status="pass" if out.status == "done" else "fail",
            score=1.0 if out.status == "done" else 0.0,
            duration_ms=round(out.latency_ms, 2),
            run_summary=RunSummary(
                model_calls=out.attempts,
                stop_reasons={out.stop_reason: 1} if out.stop_reason else {},
                model_latency_ms=round(out.latency_ms, 2),
                usage=out.usage.to_protocol(),
                step_count=1,
            ),
            coverage=RunCoverage(models_used=("bench-m",), providers_used=("openai_compatible",)),
            metrics={"attempts": float(out.attempts)},
        ))
    snap = build_benchmark_snapshot(
        snapshot_id=f"llm-bench-{int(time.time())}",
        benchmark="llm_driver_fault_bench",
        created_at_ms=int(time.time() * 1000),
        git_sha="local",
        manifest_hash="",
        metric_definitions=(MetricDefinition(key="attempts", label="Attempts", format="number", higher_is_better=False),),
        trials=tuple(trials),
    )
    print(json.dumps(to_wire(snap), ensure_ascii=False, indent=2))
    return 0 if snap.failed == 0 and snap.errored == 0 else 1


async def _cmd_replay(bundle: Path) -> int:
    replay = ReplayClient.from_tape(bundle, mode="sequence")
    count = 0
    while True:
        try:
            req = ModelRequest(provider="replay", model="replay", messages=(ModelMessage("user", f"step-{count}"),))
            out = await complete(replay, req)
            count += 1
            print(f"[{count}] status={out.status} stop={out.stop_reason} hash={out.response_hash} chars={len(out.text)}")
        except IndexError:
            break
    print(f"REPLAY_OK count={count}")
    return 0


async def _cmd_smoke(model: str, prompt: str, tape_dir: Path | None, credentials: Path | None) -> int:
    store = KimiTokenStore(path=credentials if credentials is not None else credentials_path())
    tokens = store.load()
    region = tokens.region if tokens else "mainland-cn"
    profile = make_profile(KIMI_PROFILE, base_url=api_base(region))
    req = ModelRequest(
        provider="kimi",
        model=model,
        messages=(ModelMessage(role="user", content=prompt),),
        max_tokens=512,
        trace_id="cli-smoke",
        turn_id="turn-0",
    )
    if tape_dir is not None:
        manifest = RunManifest(
            tape_id="smoke-tape",
            run_id="cli-smoke",
            session_id="cli-smoke",
            created_at_ms=int(time.time() * 1000),
            model=model,
            provider="kimi",
            components=(
                ComponentVersion(name="llm_driver", kind="llm_driver", version="1.0.0"),
                *PolicyBundle.default().component_versions(),
            ),
        )
        header = TapeHeader(tape_id="smoke-tape", session_id="cli-smoke", run_id="cli-smoke", created_at_ms=manifest.created_at_ms)
        with TapeWriter(tape_dir, header, manifest=manifest) as writer:
            driver = build_driver(profile, credentials=KimiOAuthResolver(store), recorder=LlmTapeRecorder(writer))
            try:
                out = await complete(driver, req)
            finally:
                await driver.aclose()
            writer.close(status="completed" if out.status == "done" else "failed")
    else:
        driver = build_driver(profile, credentials=KimiOAuthResolver(store))
        try:
            out = await complete(driver, req)
        finally:
            await driver.aclose()

    print(json.dumps({
        "status": out.status,
        "text": out.text,
        "ttft_ms": round(out.ttft_ms, 1) if out.ttft_ms is not None else None,
        "latency_ms": round(out.latency_ms, 1),
        "usage": to_wire(out.usage.to_protocol()),
        "error": str(out.error) if out.error else None,
    }, ensure_ascii=False, indent=2))
    return 0 if out.status == "done" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xana.llm_driver")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bench", help="run local loopback SSE & fault-injection benchmark")
    p_rep = sub.add_parser("replay", help="replay a recorded Tape bundle offline")
    p_rep.add_argument("bundle", type=Path)
    p_smk = sub.add_parser("smoke", help="run a real Kimi smoke request")
    p_smk.add_argument("--model", default="kimi-for-coding")
    p_smk.add_argument("--prompt", default="你好，只需回复两个字。")
    p_smk.add_argument("--tape-dir", type=Path, default=None)
    p_smk.add_argument("--credentials", type=Path, default=None, help="path to kimi.json credentials file")
    args = parser.parse_args(argv)

    if args.cmd == "bench":
        return asyncio.run(_cmd_bench())
    if args.cmd == "replay":
        return asyncio.run(_cmd_replay(args.bundle))
    if args.cmd == "smoke":
        return asyncio.run(_cmd_smoke(args.model, args.prompt, args.tape_dir, args.credentials))
    return 1


if __name__ == "__main__":
    sys.exit(main())
