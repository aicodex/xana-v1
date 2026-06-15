"""Event-to-state dispatch for UnifiedCardState (T9).

`apply_event(state, event)` consumes HarnessEvent / AgentEvent / ProgressEvent
and mutates `state` in place via the state-machine transitions defined in
`unified_card.py`. Pure bridge — no I/O, no adapter wiring; adapters call this
in their event loop and then hand `state` to the platform renderer.
"""
from __future__ import annotations

from typing import Any

from xana.agent_adapter.channels.unified_card import (
    AnswerSection,
    Button,
    ButtonRow,
    CompactSection,
    MenuSection,
    UnifiedCardState,
    advance_compact_input,
    advance_compact_streaming,
    append_answer,
    append_thinking,
    append_tool,
    apply_detach,
    apply_ph_replaced,
    build_attach_menu,
    build_help_menu,
    cancel_answer,
    cancel_compact,
    compute_top_buttons,
    finalize_answer,
    finalize_compact,
    ToolBlock,
)
from xana.agents.contracts import AgentEvent
from xana.harness_sdk.contracts import HarnessEvent, ProgressEvent


def apply_event(state: UnifiedCardState, event: Any) -> UnifiedCardState:
    """Dispatch any supported event type to the matching state transition.

    Returns the same `state` for convenience; mutation is in place.
    Unknown event types / kinds are no-ops.
    """
    if isinstance(event, AgentEvent):
        # Most AgentEvents wrap a HarnessEvent that carries the real payload.
        if event.harness_event is not None:
            _apply_harness(state, event.harness_event)
        else:
            _apply_agent_only(state, event)
        return state
    if isinstance(event, HarnessEvent):
        _apply_harness(state, event)
        return state
    if isinstance(event, ProgressEvent):
        _apply_progress(state, event)
        return state
    return state


# --- HarnessEvent ---------------------------------------------------------


def _apply_harness(state: UnifiedCardState, ev: HarnessEvent) -> None:
    kind = ev.kind
    payload = ev.payload or {}
    if kind == "turn_started":
        if state.answer is None:
            state.answer = AnswerSection()
        state.answer.state = "streaming"
        state.top_buttons = compute_top_buttons(state)
        return
    if kind == "model_delta":
        if state.answer is None:
            state.answer = AnswerSection()
        text = str(payload.get("text", "") or "")
        if payload.get("channel") == "thinking":
            append_thinking(state.answer, text)
        else:
            append_answer(state.answer, text)
        return
    if kind == "tool_started":
        if state.answer is None:
            state.answer = AnswerSection()
        name = str(payload.get("tool_name", "") or "")
        args = payload.get("input") or {}
        header = _tool_header(name, args)
        append_tool(
            state.answer,
            ToolBlock(name=name, header=header, body_md="", ok=True),
        )
        return
    if kind == "tool_finished":
        if state.answer is None:
            return
        call_id = str(payload.get("call_id", "") or "")
        name = str(payload.get("tool_name", "") or "")
        ok = bool(payload.get("ok", True))
        body = str(payload.get("text", "") or "")
        # find the latest matching tool block (by name) and patch body/ok.
        # tools is a tuple; rebuild with the last matching entry updated.
        tools = list(state.answer.tools)
        for i in range(len(tools) - 1, -1, -1):
            if tools[i].name == name or not name:
                tools[i] = ToolBlock(
                    name=tools[i].name,
                    header=tools[i].header,
                    body_md=body,
                    ok=ok,
                )
                break
        state.answer.tools = tuple(tools)
        _ = call_id  # currently unused — adapter may correlate later
        return
    if kind == "paused":
        # treat as transient — no state flip. Adapter may add visual hint later.
        return
    if kind == "cancelled":
        if state.answer is not None:
            cancel_answer(state.answer)
        state.top_buttons = compute_top_buttons(state)
        return
    if kind == "final":
        if state.answer is not None:
            text = str(payload.get("text", "") or state.answer.answer_text)
            finalize_answer(state.answer, final_text=text)
        state.top_buttons = compute_top_buttons(state)
        return
    if kind == "error":
        # leave answer text intact; flip to cancelled so renderer shows
        # cancelled-styled block. Adapter may decorate with error text.
        if state.answer is not None:
            cancel_answer(state.answer)
        state.top_buttons = compute_top_buttons(state)
        return


def _apply_agent_only(state: UnifiedCardState, ev: AgentEvent) -> None:
    # Agent-level events without a wrapped harness event carry results from
    # the command router (e.g. /clear, /help fallback). Treat `final`/`error`
    # payload `text` as the answer to display on the pre-built command card.
    payload = ev.payload or {}
    if ev.kind in ("final", "error"):
        text = str(payload.get("text", "") or "")
        # 命令结果是菜单卡(纯命令场景):落到 menu section,不创建 answer。
        # spec §3:菜单卡只有 menu,不出 thinking/回答。已经有 answer 的(普通 chat
        # 通过 harness 路径)走 _apply_harness final 分支,不进这里。
        if state.menu is None:
            state.menu = MenuSection(body_md=text)
        else:
            state.menu.body_md = text
        state.terminal = True
        state.footer.state = "menu"
        state.top_buttons = ()
        return
    if ev.kind == "cancelled":
        if state.answer is not None:
            cancel_answer(state.answer)
        state.top_buttons = compute_top_buttons(state)


# --- ProgressEvent --------------------------------------------------------


def _apply_progress(state: UnifiedCardState, ev: ProgressEvent) -> None:
    payload = ev.payload or {}
    kind = str(payload.get("kind", "") or ev.message or "")
    if kind == "run_started":
        if state.answer is None:
            state.answer = AnswerSection()
        state.answer.state = "streaming"
        if ev.run_id and not state.ph_run_id:
            state.ph_run_id = ev.run_id
        state.top_buttons = compute_top_buttons(state)
        return
    if kind == "run_detached":
        apply_detach(state)
        return
    if kind in ("attached", "foreground_changed"):
        # the card becomes foreground again — clear detached, restore answer
        # streaming state, recompute buttons.
        state.detached = False
        if state.answer is not None and state.answer.state == "detached":
            state.answer.state = "streaming"
        if state.footer.state == "bg_frozen":
            state.footer.state = "fg_running"
        state.top_buttons = compute_top_buttons(state)
        return
    if kind == "ph_replaced":
        final_text = str(payload.get("text", "") or payload.get("final", "") or "")
        try:
            ctx_used = int(payload.get("ctx_used", state.footer.ctx_used) or 0)
        except (TypeError, ValueError):
            ctx_used = state.footer.ctx_used
        try:
            elapsed_s = int(payload.get("elapsed_s", state.footer.elapsed_s) or 0)
        except (TypeError, ValueError):
            elapsed_s = state.footer.elapsed_s
        apply_ph_replaced(
            state,
            final_answer=final_text,
            ctx_used=ctx_used,
            elapsed_s=elapsed_s,
        )
        return
    if kind == "run_completed_bg":
        # ph_replaced typically already fired; ensure terminal state.
        if not state.terminal and state.answer is not None:
            finalize_answer(state.answer, final_text=state.answer.answer_text)
            state.footer.state = "terminal"
            state.terminal = True
            state.top_buttons = ()
        return
    if kind == "compact_started":
        if state.compact is None:
            state.compact = CompactSection(state="input")
        else:
            state.compact.state = "input"
            state.compact.progress_cells = 0
        # compact section 出现 → 清掉 loading menu(command_progress 占位),
        # spec §3:section 互斥,只 compact 或 compact+回答,不应同时有菜单。
        state.menu = None
        state.top_buttons = compute_top_buttons(state)
        return
    if kind == "compact_progress":
        if state.compact is None:
            state.compact = CompactSection(state="input")
        # Two payload shapes supported:
        #   - pct: 0..100 (Group H bar) → map to 0..10 cells
        #   - sections_done: 0..7        → use streaming advancer
        # Either form transitions input → streaming on first delta.
        if "sections_done" in payload:
            try:
                n = int(payload.get("sections_done") or 0)
            except (TypeError, ValueError):
                n = 0
            advance_compact_streaming(state.compact, sections_done=n)
        elif "pct" in payload:
            try:
                pct = int(payload.get("pct") or 0)
            except (TypeError, ValueError):
                pct = 0
            cells = max(0, min(10, pct // 10))
            if cells >= 7:
                state.compact.state = "streaming"
                state.compact.progress_cells = cells
            else:
                # still in input phase — just bump cells
                state.compact.progress_cells = cells
        elif "elapsed_s" in payload:
            try:
                es = float(payload.get("elapsed_s") or 0.0)
            except (TypeError, ValueError):
                es = 0.0
            advance_compact_input(state.compact, elapsed_s=es)
        return
    if kind == "compact_finalized":
        if state.compact is None:
            state.compact = CompactSection()
        if payload.get("cancelled"):
            cancel_compact(state.compact)
            return
        try:
            n = int(payload.get("compacted_n", 0) or 0)
        except (TypeError, ValueError):
            n = 0
        summary = str(payload.get("summary", "") or "")
        finalize_compact(state.compact, compacted_n=n, summary_text=summary)
        return
    if kind == "usage_progress":
        # Live usage updates the footer running model/ctx/elapsed; once the
        # card is terminal we freeze the footer (caller writes terminal ctx
        # via ph_replaced).
        if state.terminal:
            return
        model = payload.get("model")
        if model:
            state.footer.model = str(model)
        try:
            tin = int(payload.get("tokens_in") or 0)
        except (TypeError, ValueError):
            tin = 0
        if tin:
            state.footer.ctx_used = tin
        try:
            cw = int(payload.get("context_window") or 0)
        except (TypeError, ValueError):
            cw = 0
        if cw:
            state.footer.ctx_max = cw
        try:
            es = int(payload.get("elapsed_s") or 0)
        except (TypeError, ValueError):
            es = 0
        if es:
            state.footer.elapsed_s = es
        return
    if kind == "command_progress":
        text = payload.get("text")
        if text is not None:
            if state.menu is None:
                state.menu = MenuSection(body_md=str(text))
            else:
                state.menu.body_md = str(text)
            state.footer.state = "menu"
        return
    if kind == "menu_response":
        specs_raw = payload.get("specs") or ()
        specs = tuple(
            (str(name), str(desc)) for name, desc in specs_raw
        )
        built = build_help_menu(card_id=state.card_id, specs=specs)
        state.menu = built.menu
        state.bottom_buttons = built.bottom_buttons
        return
    if kind == "attach_options":
        cands_raw = payload.get("candidates") or ()
        cands = tuple(
            (str(run_id), str(label)) for run_id, label in cands_raw
        )
        built = build_attach_menu(card_id=state.card_id, candidates=cands)
        state.menu = built.menu
        state.bottom_buttons = built.bottom_buttons
        return


# --- helpers --------------------------------------------------------------


def _tool_header(name: str, args: dict[str, Any]) -> str:
    """Best-effort 'Read /foo.py' style header from tool name + arg hint."""
    if not args:
        return name
    # common arg keys to surface
    for key in ("path", "file", "cmd", "command", "url", "query", "pattern"):
        if key in args and args[key]:
            return f"{name} {args[key]}"
    return name
