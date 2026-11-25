"""Standalone CLI for xana.harness_sdk: `python -m xana.harness_sdk bench [--json]`."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator

from xana.context.sdk import ContextSDK
from xana.harness_sdk.contracts import (
    CompactionContext,
    HarnessContext,
    HarnessRequest,
    StopContext,
)
from xana.harness_sdk.default import DefaultHarness
from xana.harness_sdk.policies import DefaultCompactionPolicy, DefaultStopDecisionPolicy
from xana.harness_sdk.tape_recorder import HarnessTapeRecorder
from xana.llm_driver.contracts import (
    ModelEvent,
    ModelRequest,
    ProviderCapabilities,
)
from xana.llm_driver.interfaces import ModelClient, ModelRegistry
from xana.memory.active_context import InMemoryActiveContextStore
from xana.memory.file_history import FileHistoryMirror
from xana.policy.contracts import PermissionResult
from xana.protocol.eval import (
    EvalTrialRecord,
    MetricDefinition,
    RunSummary,
    build_benchmark_snapshot,
)
from xana.protocol.tape import RunManifest, TapeHeader, TapeWriter, read_tape
from xana.protocol.versioning import to_wire
from xana.tools.contracts import (
    ComponentSource,
    ComponentSpec,
    ToolContext,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)
from xana.tools.interfaces import Tool
from xana.tools.registry import InMemoryToolRegistry


class _ScriptedClient(ModelClient):
    """Deterministic offline scripted ModelClient for standalone harness bench."""

    def __init__(self) -> None:
        self._turn = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="scripted", supports_streaming=True, supports_tools=True)

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        self._turn += 1
        if self._turn == 1:
            yield ModelEvent(
                kind="tool_call",
                payload={"call_id": "c1", "tool_name": "echo_tool", "input": {"msg": "ping"}},
            )
            yield ModelEvent(kind="done", payload={"stop_reason": "tool_calls"})
            return
        yield ModelEvent(kind="text_delta", payload={"text": "echo_tool returned: pong"})
        yield ModelEvent(kind="done", payload={"stop_reason": "stop"})

    async def cancel(self, request_id: str) -> None:
        return None


class _SimpleRegistry(ModelRegistry):
    def __init__(self, client: ModelClient) -> None:
        self._client = client

    async def register(self, provider: str, client: ModelClient) -> None:
        self._client = client

    async def get(self, provider: str) -> ModelClient | None:
        return self._client

    async def list_providers(self) -> tuple[str, ...]:
        return ("scripted",)


class _EchoTool(Tool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            component=ComponentSpec(
                kind="tool",
                name="echo_tool",
                qualified_name="bench.echo_tool",
                description="Echo tool for standalone harness bench.",
                source=ComponentSource(source_type="builtin", scope="system"),
            ),
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        )

    async def validate(self, invocation: ToolInvocation) -> None:
        return None

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="bench")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, content={"reply": "pong"}, rendered="pong")


async def _run_bench():
    trials: list[EvalTrialRecord] = []

    def _add(task_id: str, ok: bool, duration_ms: float) -> None:
        trials.append(
            EvalTrialRecord(
                trial_id=f"trial-{task_id}",
                benchmark="harness_sdk_bench",
                task_id=task_id,
                split="train",
                status="pass" if ok else "fail",
                score=1.0 if ok else 0.0,
                duration_ms=round(duration_ms, 2),
                run_summary=RunSummary(step_count=1),
            )
        )

    with tempfile.TemporaryDirectory(prefix="xana_harness_bench_") as tmp:
        root = Path(tmp)
        bundle = root / "tape"
        now_ms = int(time.time() * 1000)
        comp_policy = DefaultCompactionPolicy()
        stop_policy = DefaultStopDecisionPolicy()
        manifest = RunManifest(
            tape_id="t-har",
            run_id="r-har",
            session_id="s-har",
            created_at_ms=now_ms,
            model="scripted-model",
            provider="scripted",
            components=(comp_policy.component_version(), stop_policy.component_version()),
        )
        header = TapeHeader(tape_id="t-har", session_id="s-har", run_id="r-har", created_at_ms=now_ms)

        with TapeWriter(bundle, header, manifest=manifest) as writer:
            recorder = HarnessTapeRecorder(writer)

            # 1. Standalone multi-turn tool loop without host/boot
            t0 = time.monotonic()
            sdk = ContextSDK()
            active_store = InMemoryActiveContextStore(sdk)
            mirror = FileHistoryMirror(root / "history")
            tool_reg = InMemoryToolRegistry()
            await tool_reg.register(_EchoTool())
            model_reg = _SimpleRegistry(_ScriptedClient())
            harness = DefaultHarness(
                compaction_policy=comp_policy,
                stop_policy=stop_policy,
                tape_recorder=recorder,
            )
            ctx = HarnessContext(
                session_id="s-har",
                agent_name="bench",
                cwd=str(root),
                model="scripted-model",
                active_context_store=active_store,
                history_mirror=mirror,
                tool_registry=tool_reg,
                model_registry=model_reg,
                config_snapshot={"provider": "scripted", "model": "scripted-model"},
            )
            req = HarnessRequest(session_id="s-har", agent_name="bench", user_input="run echo_tool")
            events = [ev async for ev in harness.invoke(req, ctx)]
            kinds = [ev.kind for ev in events]
            final_ev = next((ev for ev in events if ev.kind == "final"), None)
            ok1 = (
                "tool_started" in kinds
                and "tool_finished" in kinds
                and final_ev is not None
                and "pong" in str(final_ev.payload.get("text", ""))
            )
            _add("multi_turn_tool_loop", ok1, (time.monotonic() - t0) * 1000.0)

            # 2. CompactionPolicy threshold & blocking decisions
            t1 = time.monotonic()
            c_ctx_compact = CompactionContext(
                estimated_tokens=90000,
                context_window=100000,
                force_compact=80000,
                blocking_limit=95000,
                history_len=10,
            )
            d_compact = comp_policy.evaluate(c_ctx_compact)
            recorder.record_compaction(
                session_id="s-har", run_id="r-har", ctx=c_ctx_compact, decision=d_compact, changed=True,
                policy=comp_policy.component_version(),
            )
            d_block = comp_policy.evaluate(
                CompactionContext(
                    estimated_tokens=98000,
                    context_window=100000,
                    force_compact=80000,
                    blocking_limit=95000,
                    history_len=10,
                    compaction_blocked=True,
                )
            )
            ok2 = d_compact.action == "compact" and d_block.action == "block"
            _add("compaction_policy_decisions", ok2, (time.monotonic() - t1) * 1000.0)

            # 3. StopDecisionPolicy cascade & tape round-trip
            t2 = time.monotonic()
            d_think = stop_policy.decide_stop(
                StopContext(turn_text="", had_thinking=True, stop_reason="stop")
            )
            d_trunc = stop_policy.decide_stop(
                StopContext(turn_text="partial", had_thinking=False, stop_reason="length", truncations=0)
            )
            d_limit = stop_policy.decide_stop(
                StopContext(
                    turn_text="done", had_thinking=False, stop_reason="stop",
                    hook_blocks=8, hook_block_reason="still blocked", goal_active=True,
                )
            )
            rem = stop_policy.decide_silent_reminder(silent_calls=5, silent_reminders=0, is_child_run=False)
            rem_child = stop_policy.decide_silent_reminder(silent_calls=5, silent_reminders=0, is_child_run=True)
            writer.close(status="completed")

        tape = read_tape(bundle)
        event_types = {e.type for e in tape.events}
        ok3 = (
            d_think.action == "continue"
            and d_think.mark_thinking_nudged
            and d_trunc.action == "continue"
            and d_trunc.increment_truncations
            and d_limit.action == "stop"
            and d_limit.pause_goal_reason == "stop_hook_or_verification_limit"
            and rem is not None
            and rem_child is None
            and tape.status == "complete"
            and {"harness.compaction", "harness.stop_decision"}.issubset(event_types)
        )
        _add("stop_decision_policy_and_tape", ok3, (time.monotonic() - t2) * 1000.0)

    return build_benchmark_snapshot(
        snapshot_id=f"harness-bench-{int(time.time())}",
        benchmark="harness_sdk_bench",
        created_at_ms=int(time.time() * 1000),
        git_sha="local",
        manifest_hash="",
        metric_definitions=(MetricDefinition(key="pass_rate", label="Pass Rate", format="percent"),),
        trials=tuple(trials),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xana.harness_sdk")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bench_p = sub.add_parser("bench", help="Run standalone Harness SDK & Context benchmark")
    bench_p.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON snapshot")
    args = parser.parse_args(argv)
    if args.cmd == "bench":
        snap = asyncio.run(_run_bench())
        if args.as_json:
            print(json.dumps(to_wire(snap), ensure_ascii=False, indent=2))
        else:
            rate = snap.passed / snap.total if snap.total else 0.0
            print(f"[xana.harness_sdk bench] {snap.passed}/{snap.total} passed (pass_rate={rate:.2f})")
            for t in snap.trials:
                print(f"  - {t.task_id}: {t.status.upper()} ({t.duration_ms:.2f}ms)")
        return 0 if snap.failed == 0 and snap.errored == 0 else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
