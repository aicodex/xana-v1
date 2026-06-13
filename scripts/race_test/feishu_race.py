"""Feishu race-test driver. Drives the full pipeline (gateway + chat_agent +
harness) but with a fake feishu api_sender that records every card payload,
so we can validate detach/switch behavior without hitting real Feishu.

Usage:
    python scripts/race_test/feishu_race.py [a|c]

a = detach scenario (U1 → U2)
c = switch_foreground scenario (U1 → U2 → switch back)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from slow_task_tool import SlowTaskTool  # noqa: E402

from xana.agent_adapter.channels._bindings import BindingStore  # noqa: E402
from xana.agent_adapter.channels._pairing import PairingStore  # noqa: E402
from xana.agent_adapter.channels.feishu import FeishuAdapter, FeishuConfig  # noqa: E402
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
_PROMPT_HELLO = "你好。简短回答。"


class _FakeFeishuSender:
    """Drop-in for the real httpx Feishu sender. Records all card payloads."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, dict]] = []
        self.patches: list[tuple[str, dict]] = []
        self._seq = 0

    async def send_card(self, *, receive_id: str, card: dict) -> str:
        self._seq += 1
        mid = f"om_fake_{self._seq:03d}"
        self.sends.append((mid, card))
        return mid

    async def patch_card(self, *, message_id: str, card: dict) -> None:
        import traceback
        # capture caller info per patch so we can correlate streaming-after-frozen
        stack = traceback.extract_stack()
        # find the immediate caller above this method (skip itself + asyncio wrappers)
        caller = "?"
        for fr in reversed(stack[:-1]):
            if "feishu" in fr.filename or "throttle" in fr.filename:
                caller = f"{Path(fr.filename).name}:{fr.lineno}"
                break
        self.patches.append((message_id, card))
        if not hasattr(self, "patch_callers"):
            self.patch_callers = []  # type: ignore[attr-defined]
        self.patch_callers.append((message_id, caller))

    async def send_message(self, *, receive_id: str, text: str) -> str:
        self._seq += 1
        return f"om_fake_msg_{self._seq:03d}"


class _Issue:
    def __init__(self, severity: str, msg: str) -> None:
        self.severity = severity  # "ERROR" | "WARN"
        self.msg = msg

    def __str__(self) -> str:
        return f"  [{self.severity}] {self.msg}"


def _has_stop_button(card: dict) -> bool:
    """Look for a ⏹ stop button in any column_set in the card body."""
    for el in card.get("body", {}).get("elements", []):
        if el.get("tag") != "column_set":
            continue
        for col in el.get("columns", []):
            for sub in col.get("elements", []):
                text = (sub.get("text") or {}).get("content", "")
                if "停止" in text or "⏹" in text:
                    return True
    return False


def _switch_back_targets(card: dict) -> list[str]:
    """Return list of run_ids that switch-back buttons target."""
    out = []
    for el in card.get("body", {}).get("elements", []):
        if el.get("tag") != "column_set":
            continue
        for col in el.get("columns", []):
            for sub in col.get("elements", []):
                for beh in sub.get("behaviors") or []:
                    val = beh.get("value") or {}
                    if val.get("action") == "switch_foreground":
                        out.append(val.get("run_id", ""))
    return out


def _is_frozen_card(card: dict) -> bool:
    title = ((card.get("header") or {}).get("title") or {}).get("content", "")
    return "运行中" in title


def _is_completed_bg_card(card: dict) -> bool:
    title = ((card.get("header") or {}).get("title") or {}).get("content", "")
    return "已完成" in title or "完成" in title


def _validate_card_jsonable(card: dict) -> list[str]:
    """Cheap structural validation — does it round-trip through JSON and have
    schema=2.0 + body.elements list?"""
    errs = []
    try:
        json.dumps(card)
    except (TypeError, ValueError) as e:
        errs.append(f"not JSON-serializable: {e}")
    if card.get("schema") != "2.0":
        errs.append(f"schema != 2.0: {card.get('schema')!r}")
    body = card.get("body") or {}
    elements = body.get("elements")
    if not isinstance(elements, list):
        errs.append(f"body.elements not a list: {type(elements).__name__}")
    return errs


async def _setup() -> tuple[FeishuAdapter, object, _FakeFeishuSender]:
    project_root = Path(os.environ.get("XANA_HOME", str(Path.home() / ".xana"))) / "workspace"
    project_root.mkdir(parents=True, exist_ok=True)
    history_root = Path(os.environ.get("XANA_HOME", str(Path.home() / ".xana"))) / "race_sessions"
    history_root.mkdir(parents=True, exist_ok=True)

    cp = DefaultConfigProvider()
    boot_result = await boot(
        project_root=project_root, config_provider=cp, history_root=history_root,
    )

    # Replace chat agent with only-slow-task tools, no skills.
    test_tools = InMemoryToolRegistry()
    await test_tools.register(SlowTaskTool())
    test_skills = InMemorySkillRegistry()
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

    # Build feishu adapter wired with FAKE sender so we don't hit network.
    fake_sender = _FakeFeishuSender()
    cfg = FeishuConfig(
        app_id="fake", app_secret="fake", verification_token="fake",
        api_base="https://open.feishu.cn",
    )
    pairing = PairingStore(path=Path("/tmp") / "race_pairings.json")
    bindings = BindingStore(path=Path("/tmp") / "race_feishu.bindings.json")
    adapter = FeishuAdapter(
        config=cfg, bindings=bindings, pairing=pairing, api_sender=fake_sender,
    )
    adapter.set_gateway(boot_result.gateway)
    await boot_result.gateway.register_adapter(adapter.adapter_id, adapter)

    # Pre-bind a fake feishu session: tell adapter chat_id maps to a session.
    sid = "feishu-race"
    buf = adapter._get_or_create_buffer(sid)
    buf.chat_id = "oc_fake_chat"

    print(f"[setup] model={boot_result.provider}/{boot_result.model}")
    return adapter, boot_result.gateway, fake_sender


async def _submit_bg(gateway, adapter, sid: str, text: str) -> asyncio.Task:
    inbound = InboundMessage(adapter_id=adapter.adapter_id, session_id=sid, text=text)

    async def _drain():
        async for _ in gateway.submit(inbound):
            pass
    return asyncio.create_task(_drain())


def _audit_after_detach(fake: _FakeFeishuSender) -> list[_Issue]:
    """After scenario A: run1 should have been patched to frozen state (no
    stop button, no switch-back-to-self). run2 should be streaming normally."""
    issues = []
    # All sends must validate
    for mid, card in fake.sends + [(m, c) for m, c in fake.patches]:
        errs = _validate_card_jsonable(card)
        for e in errs:
            issues.append(_Issue("ERROR", f"card {mid} invalid: {e}"))

    # Find run1's first frozen patch
    frozen_seen = False
    for mid, card in fake.patches:
        if _is_frozen_card(card):
            frozen_seen = True
            if _has_stop_button(card):
                issues.append(_Issue("ERROR", f"frozen card {mid} still has stop button"))
            targets = _switch_back_targets(card)
            if targets:
                # Frozen card shouldn't have switch-back buttons
                issues.append(_Issue("ERROR",
                    f"frozen card {mid} has switch-back buttons: {targets}"))
    if not frozen_seen:
        issues.append(_Issue("ERROR", "no frozen card was patched — detach didn't reach feishu"))
    return issues


def _audit_after_switch(fake: _FakeFeishuSender, run1_id: str, run2_id: str) -> list[_Issue]:
    """After scenario C: switching back to run1 should have created a new send
    with run2 in its switch-back buttons. Card payloads must be schema-valid.

    Also assert: each card's patch sequence — once frozen, no further patches
    should change the card content."""
    issues = []
    for mid, card in fake.sends + fake.patches:
        errs = _validate_card_jsonable(card)
        for e in errs:
            issues.append(_Issue("ERROR", f"card {mid} invalid: {e}"))

    # Group patches by message_id, in order
    per_mid: dict[str, list[dict]] = {}
    for mid, card in fake.patches:
        per_mid.setdefault(mid, []).append(card)
    # For each mid: find FIRST frozen patch index, then check that NO subsequent
    # unfrozen patch arrives, and no body-content change after frozen.
    for mid, patches in per_mid.items():
        first_frozen_idx = None
        for i, c in enumerate(patches):
            if _is_frozen_card(c):
                first_frozen_idx = i
                break
        if first_frozen_idx is None:
            continue
        first_frozen_body = json.dumps(patches[first_frozen_idx].get("body"), ensure_ascii=False)
        for j, c in enumerate(patches[first_frozen_idx + 1:], start=first_frozen_idx + 1):
            if _is_frozen_card(c):
                body = json.dumps(c.get("body"), ensure_ascii=False)
                if body != first_frozen_body:
                    issues.append(_Issue("WARN",
                        f"card {mid} patch #{j} still frozen but body changed "
                        f"(perceived as streaming) — diff={len(body) - len(first_frozen_body)}"))
            elif _is_completed_bg_card(c):
                # ph_replaced 把冻结卡换成"后台已完成"卡 — expected transition
                pass
            else:
                issues.append(_Issue("ERROR",
                    f"card {mid} patch #{j} reverted from frozen to streaming"))
    print("\n  patch counts per message_id:")
    for mid, ps in per_mid.items():
        frozen_count = sum(1 for c in ps if _is_frozen_card(c))
        print(f"    {mid}: {len(ps)} patches ({frozen_count} frozen)")
    print("\n  patch sequence for om_fake_002:")
    callers = getattr(fake, "patch_callers", [])
    mid_callers = [c for m, c in callers if m == "om_fake_002"]
    for i, c in enumerate(per_mid.get("om_fake_002", [])):
        body_len = len(json.dumps(c.get("body"), ensure_ascii=False))
        title = ((c.get("header") or {}).get("title") or {}).get("content", "")
        flag = "FROZEN" if _is_frozen_card(c) else "stream"
        src = mid_callers[i] if i < len(mid_callers) else "?"
        print(f"    #{i}: {flag} body_len={body_len} src={src} title={title!r}")
    # Look for a non-frozen send card after the switch that has switch-back to run2
    found_switch_back_to_run2 = False
    for _mid, card in fake.sends:
        if _is_frozen_card(card):
            continue
        targets = _switch_back_targets(card)
        if run2_id in targets:
            found_switch_back_to_run2 = True
            # And NOT to itself
            if run1_id in targets:
                issues.append(_Issue("ERROR",
                    f"card switch-back includes self ({run1_id}) — bg_runs not filtered"))
    if not found_switch_back_to_run2:
        issues.append(_Issue("WARN",
            f"no card with switch-back to {run2_id} — switch may not have run yet"))
    return issues


# ---------- scenarios ----------

async def scenario_a(adapter, gateway, fake) -> list[_Issue]:
    sid = "feishu-race"
    print("\n=== A: detach mid-tool-chain ===")
    t1 = await _submit_bg(gateway, adapter, sid, _PROMPT_3X_5S)
    print("  U1 submitted, sleeping 10s ...")
    await asyncio.sleep(10)
    print("  sending U2 ...")
    t2 = await _submit_bg(gateway, adapter, sid, _PROMPT_HELLO)
    print("  awaiting both ...")
    await asyncio.gather(t1, t2)
    print(f"  sends={len(fake.sends)} patches={len(fake.patches)}")
    return _audit_after_detach(fake)


async def scenario_c(adapter, gateway, fake) -> list[_Issue]:
    sid = "feishu-race"
    print("\n=== C: switch back mid-flight ===")
    t1 = await _submit_bg(gateway, adapter, sid, _PROMPT_3X_5S)
    await asyncio.sleep(8)
    print("  sending U2 ...")
    t2 = await _submit_bg(gateway, adapter, sid, _PROMPT_HELLO)
    await asyncio.sleep(4)
    # Find run_ids from state
    state = adapter._render_states[sid]
    run_ids = list(state.runs.keys())
    print(f"  state.runs={run_ids}, fg={state.foreground_run_id}")
    bg_id = next(r for r in run_ids if r != state.foreground_run_id)
    fg_id = state.foreground_run_id
    print(f"  switching foreground back to {bg_id} (away from {fg_id}) ...")
    await gateway.switch_foreground(sid, bg_id)
    # Let _send_full_card execute
    await asyncio.sleep(1)
    print(f"  sends={len(fake.sends)} patches={len(fake.patches)}")
    await asyncio.gather(t1, t2)
    return _audit_after_switch(fake, run1_id=bg_id, run2_id=fg_id)


SCENARIOS = {"a": scenario_a, "c": scenario_c}


async def main() -> int:
    pick = sys.argv[1] if len(sys.argv) > 1 else "a"
    fn = SCENARIOS.get(pick)
    if fn is None:
        print(f"unknown scenario: {pick} (have: {list(SCENARIOS)})")
        return 2
    adapter, gateway, fake = await _setup()
    try:
        issues = await fn(adapter, gateway, fake)
    finally:
        await adapter.stop()

    print("\n--- Audit ---")
    if not issues:
        print("  ✅ no issues")
        return 0
    for it in issues:
        print(it)
    errors = sum(1 for i in issues if i.severity == "ERROR")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
