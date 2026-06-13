"""Race-test driver — manual heavy E2E for PH-driven adapter concurrency.

Boots a stripped agent (only `test_slow_task` tool, no skills) and runs scripted
scenarios with long sleeps to isolate the race window from LLM latency.

USAGE
    python scripts/race_test/run.py [scenario_name]

If no scenario name given, runs all scenarios in sequence.

Heavy: makes real LLM calls. Do not run by default.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# project root on path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from slow_task_tool import SlowTaskTool  # noqa: E402

from xana.agent_adapter.cli import CliAdapter  # noqa: E402
from xana.agent_adapter.contracts import InboundMessage  # noqa: E402
from xana.agents.chat_agent import ChatAgent, build_chat_agent_spec  # noqa: E402
from xana.config.loader import DefaultConfigProvider  # noqa: E402
from xana.harness_sdk.default import DefaultHarness  # noqa: E402
from xana.runtime.boot import boot  # noqa: E402
from xana.skills.registry import InMemorySkillRegistry  # noqa: E402
from xana.tools.registry import InMemoryToolRegistry  # noqa: E402


_PROMPT_3X_5S = (
    "请调用 test_slow_task 工具三次,每次参数 delay_s=5,label 分别为 a/b/c。"
    "调用工具,不要解释,直接调。"
)
_PROMPT_1X_20S = (
    "请调用 test_slow_task 工具一次,参数 delay_s=20, label='long'。"
    "调用工具,不要解释,直接调。"
)
_PROMPT_HELLO = "你好。简短回答。"


class _EventLog:
    def __init__(self) -> None:
        self.entries: list[tuple[float, str, dict]] = []
        self._t0 = time.monotonic()

    def push(self, kind: str, payload: dict) -> None:
        self.entries.append((time.monotonic() - self._t0, kind, dict(payload)))

    def dump(self) -> str:
        lines = []
        for t, kind, p in self.entries:
            run_id = p.get("run_id", "")
            extras = {k: v for k, v in p.items() if k not in {"run_id", "session_id", "draft", "text"}}
            lines.append(f"  t={t:6.2f}s  {kind:<22s}  run={run_id:<28s}  {extras}")
        return "\n".join(lines)


class _LoggingAdapter(CliAdapter):
    """Wraps CliAdapter, logging note_progress + render events."""

    def __init__(self, log: _EventLog, **kwargs):
        super().__init__(**kwargs)
        self._log = log

    def note_progress(self, payload: dict) -> None:  # type: ignore[override]
        self._log.push(payload.get("kind", "?"), payload)
        super().note_progress(payload)

    async def render(self, event):  # type: ignore[override]
        payload = dict(event.payload)
        if event.kind in {"final", "error", "tool_finished", "tool_started"}:
            self._log.push(f"render.{event.kind}", payload)
        return await super().render(event)


async def _setup() -> tuple[object, _LoggingAdapter, _EventLog]:
    """Boot pipeline, swap chat agent for a minimal one with only SlowTaskTool."""
    project_root = Path(os.environ.get("XANA_HOME", str(Path.home() / ".xana"))) / "workspace"
    project_root.mkdir(parents=True, exist_ok=True)
    history_root = Path(os.environ.get("XANA_HOME", str(Path.home() / ".xana"))) / "race_sessions"
    history_root.mkdir(parents=True, exist_ok=True)

    cp = DefaultConfigProvider()
    boot_result = await boot(
        project_root=project_root,
        config_provider=cp,
        history_root=history_root,
    )

    # Build minimal tool registry: only SlowTaskTool.
    test_tools = InMemoryToolRegistry()
    await test_tools.register(SlowTaskTool())

    # Empty skill registry.
    test_skills = InMemorySkillRegistry()

    # Reconstruct chat agent with overridden registries.
    chat_agent = ChatAgent(
        spec=build_chat_agent_spec(),
        harness=DefaultHarness(extension_registry=None),
        active_context_store=boot_result.active_context_store,
        history_mirror=boot_result.history_mirror,
        memory_injector_registry=boot_result.memory_injector_registry,
        tool_registry=test_tools,
        skill_registry=test_skills,
        model_registry=boot_result.model_registry,
        state_repository=boot_result.state_repository,
        cwd=str(project_root),
        model=boot_result.model,
        provider=boot_result.provider,
        session_config_store=boot_result.session_config_store,
        progress_sink=boot_result.progress_sink,
        main_writer=boot_result.main_writer,
        run_jsonl_writer=boot_result.run_jsonl_writer,
    )
    boot_result.gateway.agents["chat"] = chat_agent

    log = _EventLog()
    adapter = _LoggingAdapter(log=log, adapter_id="cli", session_id="race-sess")
    await adapter.start()
    await boot_result.gateway.register_adapter(adapter.adapter_id, adapter)

    print(f"[setup] model={boot_result.provider}/{boot_result.model}, project_root={project_root}")
    return boot_result.gateway, adapter, log


async def _submit_bg(gateway, adapter, text: str) -> asyncio.Task:
    """Submit + drain in background; returns the task."""
    inbound = InboundMessage(adapter_id=adapter.adapter_id, session_id=adapter.session_id, text=text)

    async def _drain():
        async for _ in gateway.submit(inbound):
            pass
    return asyncio.create_task(_drain())


# ----- scenarios -----

async def scenario_a_detach_during_tool(gateway, adapter, log: _EventLog):
    """U1 fires three 5s tool calls; 10s later U2 detaches it. Expect:
    - run1 detached event
    - run1 keeps producing tool_finished events in bg (or its tasks get cancelled)
    - run2 fg ph_replaced
    - run1 ph_replaced fires after its tool chain completes
    """
    print("\n=== Scenario A: detach mid-tool-chain ===")
    t1 = await _submit_bg(gateway, adapter, _PROMPT_3X_5S)
    print("  U1 submitted, sleeping 10s ...")
    await asyncio.sleep(10)
    print("  sending U2 (hello) to detach ...")
    t2 = await _submit_bg(gateway, adapter, _PROMPT_HELLO)
    # wait long enough for both to finish — 3x 5s tools + LLM + buffer
    print("  awaiting both ...")
    await asyncio.gather(t1, t2)
    print(log.dump())


async def scenario_b_triple_submit(gateway, adapter, log: _EventLog):
    """U1, sleep 8, U2, sleep 8, U3. Verify 2 detach events, all 3 ph_replaced."""
    print("\n=== Scenario B: triple submit ===")
    t1 = await _submit_bg(gateway, adapter, _PROMPT_3X_5S)
    await asyncio.sleep(8)
    t2 = await _submit_bg(gateway, adapter, _PROMPT_3X_5S)
    await asyncio.sleep(8)
    t3 = await _submit_bg(gateway, adapter, _PROMPT_HELLO)
    await asyncio.gather(t1, t2, t3)
    print(log.dump())


async def scenario_c_switch_back(gateway, adapter, log: _EventLog):
    """U1 (long 20s tool), sleep 8, U2 (hello). Mid-flight (4s into U2), switch
    foreground back to U1 — both runs still in _inflight, so the event fires."""
    print("\n=== Scenario C: switch back to bg mid-flight ===")
    t1 = await _submit_bg(gateway, adapter, _PROMPT_1X_20S)
    await asyncio.sleep(8)
    t2 = await _submit_bg(gateway, adapter, _PROMPT_HELLO)
    await asyncio.sleep(4)  # 4s into U2, both still inflight
    run1_id = None
    for _, kind, payload in log.entries:
        if kind == "run_detached":
            run1_id = payload.get("run_id")
            break
    print(f"  Mid-flight: switching foreground back to {run1_id} ...")
    if run1_id:
        await gateway.switch_foreground(adapter.session_id, run1_id)
    await asyncio.gather(t1, t2)
    print(log.dump())


SCENARIOS = {
    "a": scenario_a_detach_during_tool,
    "b": scenario_b_triple_submit,
    "c": scenario_c_switch_back,
}


async def main() -> int:
    pick = sys.argv[1] if len(sys.argv) > 1 else None
    gateway, adapter, log = await _setup()
    try:
        if pick:
            if pick not in SCENARIOS:
                print(f"unknown scenario: {pick} (have: {list(SCENARIOS)})")
                return 2
            await SCENARIOS[pick](gateway, adapter, log)
        else:
            for name, fn in SCENARIOS.items():
                log.entries.clear()
                log._t0 = time.monotonic()  # type: ignore[attr-defined]
                await fn(gateway, adapter, log)
    finally:
        await adapter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
