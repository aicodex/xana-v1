"""Event-to-state dispatch for UnifiedCardState (T9).

`apply_event(state, event)` consumes HarnessEvent / AgentEvent / ProgressEvent
and mutates `state` in place via the state-machine transitions defined in
`unified_card.py`. Pure bridge — no I/O, no adapter wiring; adapters call this
in their event loop and then hand `state` to the platform renderer.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Any


# 菜单/命令计时:command_progress 启动,final/error/compact_finalized 停止。
# key = (session_id, card_id)。模块级 dict 简单(单进程 OK,测试隔离用 clear)。
_menu_start_ts: dict[tuple[str, str], float] = {}


def _menu_started_at(state: UnifiedCardState, session_id: str) -> None:
    _menu_start_ts[(session_id, state.card_id)] = time.monotonic()


def _menu_elapsed(state: UnifiedCardState, session_id: str) -> float:
    started = _menu_start_ts.pop((session_id, state.card_id), None)
    if started is None:
        return 0.0
    return max(0.0, time.monotonic() - started)


def _parse_menu_label(text: str) -> str:
    """从 '⏳ /clear' / '⏳ /compact' 解析出 label='clear' / 'compact'。"""
    txt = (text or "").strip()
    for token in txt.replace("⏳", " ").split():
        if token.startswith("/"):
            return token.lstrip("/")
    return txt[:20] if txt else ""

from xana.agent_adapter.channels.unified_card import (
    set_progress,
    AnswerSection,
    Button,
    ButtonRow,
    CompactSection,
    MenuSection,
    UnifiedCardState,
    advance_compact_input,
    advance_compact_streaming,
    append_answer,
    append_step,
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
from xana.agent_adapter.channels.process_view import VIEW_MODES
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
            append_step(state.answer, "think")
        else:
            append_answer(state.answer, text)
            state.answer._in_thinking = False
        return
    if kind == "progress_update":
        # 模型在工具轮写的进度说明:显示在进度行;显示模式下开启一个新过程组并作为组标题
        if state.answer is not None:
            text = str(payload.get("text", "") or "")
            set_progress(state.answer, text)
            if text.strip():
                append_step(state.answer, "caption", " ".join(text.split()))
        return
    if kind == "tool_progress":
        # 工具执行中上报的进度(如"正在阅读 18/40 个网页"):进度行 + 该调用的实时详情
        if state.answer is not None:
            text = str(payload.get("text", "") or "")
            set_progress(state.answer, text)
            _set_tool_live(state.answer, str(payload.get("call_id", "") or ""), text)
        return
    if kind == "tool_started":
        if state.answer is None:
            state.answer = AnswerSection()
        name = str(payload.get("tool_name", "") or "")
        call_id = str(payload.get("call_id", "") or "")
        args = payload.get("input") or {}
        display = payload.get("display") or {}
        mode = payload.get("view_mode")
        if mode in VIEW_MODES:   # 会话里 /view 设过就覆盖渠道默认
            state.view_mode = mode
        summary = _tool_summary(args, tuple(display.get("summary_args") or ()))
        header = _make_tool_header(name, summary, None)
        append_tool(
            state.answer,
            ToolBlock(name=name, header=header, body_md="",
                      ok=True, call_id=call_id,
                      started_at=time.monotonic(), summary=summary,
                      category=str(display.get("category") or "other"),
                      verb=str(display.get("verb") or name),
                      unit=str(display.get("unit") or "次"),
                      count=int(display.get("count") or 1)),
        )
        if call_id:
            append_step(state.answer, "tool", call_id)
        return
    if kind == "tool_finished":
        if state.answer is None:
            return
        call_id = str(payload.get("call_id", "") or "")
        name = str(payload.get("tool_name", "") or "")
        ok = bool(payload.get("ok", True))
        body = str(payload.get("text", "") or "")

        def _match(t: ToolBlock) -> bool:
            if call_id and t.call_id:
                return t.call_id == call_id
            return t.name == name or not name

        # 优先匹 sec.tools(已渲染),没中再匹 buf(按钮后的)。
        # 不带 body 的 tool 优先,避免重复 finished 覆盖已完成的。
        for collection_name in ("tools", "_pending_tools_buf"):
            items = list(getattr(state.answer, collection_name))
            # 找最后一个匹的且 body_md 为空(未完成)的
            target = -1
            for i in range(len(items) - 1, -1, -1):
                if _match(items[i]) and not items[i].body_md:
                    target = i
                    break
            if target == -1:
                # fallback:匹最后一个(允许覆盖已完成)
                for i in range(len(items) - 1, -1, -1):
                    if _match(items[i]):
                        target = i
                        break
            if target >= 0:
                old = items[target]
                cancelled = bool(payload.get("cancelled"))
                if cancelled:
                    status = "cancel"
                elif ok:
                    status = "ok"
                else:
                    status = "fail"
                # harness 报的 elapsed_ms 优先,缺则用 card 自记起点。
                ems = payload.get("elapsed_ms")
                if isinstance(ems, (int, float)) and ems >= 0:
                    elapsed_s = float(ems) / 1000.0
                elif old.started_at:
                    elapsed_s = time.monotonic() - old.started_at
                else:
                    elapsed_s = 0.0
                new_header = _make_tool_header(
                    old.name, old.summary, elapsed_s, status)
                items[target] = replace(old, header=new_header, body_md=body, ok=ok,
                                        status=status, elapsed_s=elapsed_s, live="")
                setattr(state.answer, collection_name, tuple(items))
                return
        return
    if kind == "paused":
        # treat as transient — no state flip. Adapter may add visual hint later.
        return
    if kind == "cancelled":
        if state.answer is not None:
            cancel_answer(state.answer)
            _mark_running_tools_cancelled(state.answer)
        state.footer.cancelled = True   # footer 终态 dot 改 🔴
        state.top_buttons = compute_top_buttons(state)
        return
    if kind == "final":
        if state.answer is not None:
            text = str(payload.get("text", "") or state.answer.answer_text)
            finalize_answer(state.answer, final_text=text)
        state.top_buttons = compute_top_buttons(state)
        return
    if kind == "error":
        # 出错不是取消:回答区写明原因(保留已有输出),脚标红点。
        # 以前翻成"回答已取消"且不带原因,用户看到的是"被取消了"(如 kimi token 刷新失败)。
        msg = str(payload.get("error") or payload.get("text") or "未知错误").strip()
        if state.answer is None:
            state.answer = AnswerSection()
        prev = state.answer.answer_text.rstrip()
        finalize_answer(state.answer, final_text=(prev + "\n\n" if prev else "") + f"⚠️ 出错:{msg}")
        state.footer.cancelled = True
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
        # 命令结果可带 buttons(/help /model /effort 等):转 bottom_buttons。
        # 飞书只能靠按钮回发命令;TG 也用按钮(linkify 由 TG 适配器在 render 时
        # 决定是否隐去)。每项 = {"text": str, "send": str}。
        rows_raw = payload.get("buttons") or ()
        if rows_raw:
            rows: list[ButtonRow] = []
            for row in rows_raw:
                btns = tuple(
                    Button(text=str(b.get("text") or ""),
                           send=str(b.get("send") or "") or None)
                    for b in row if isinstance(b, dict)
                )
                if btns:
                    rows.append(ButtonRow(buttons=btns))
            state.bottom_buttons = tuple(rows)
        if payload.get("linkify_commands"):
            state.linkify_commands = True
        # 菜单/命令计时落点:command_progress 起 → final/error 停。
        if state.footer.menu_label:
            state.footer.menu_elapsed_s = _menu_elapsed(state, ev.session_id or "")
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
        # cancelled 优先看 payload,再看 footer.cancelled(之前 HarnessEvent 已设)
        was_cancelled = bool(payload.get("cancelled")) or state.footer.cancelled
        apply_ph_replaced(
            state,
            final_answer=final_text,
            ctx_used=ctx_used,
            elapsed_s=elapsed_s,
            cancelled=was_cancelled,
        )
        return
    if kind == "run_completed_bg":
        # ph_replaced typically already fired; ensure terminal state.
        if not state.terminal and state.answer is not None:
            if state.detached:
                state.terminal_from_bg = True
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
        # /compact 单独卡:answer 段空(run_started 会预建 AnswerSection,
        # 但 compact 没有真实回答内容)→ 抹掉 answer + footer 走 menu 态。
        # 避免出现 "🟡 · 回答用时 0s"。
        empty_answer = (
            state.answer is None
            or (not state.answer.answer_text
                and not state.answer.thinking_text
                and not state.answer.tools)
        )
        if empty_answer:
            state.answer = None
            state.footer.state = "menu"
        # 启动 "compact" 菜单段计时;footer 同时维持 fg/bg/terminal 状态,
        # 菜单段渲染在最右(解决 compact + bg 竞态)。
        state.footer.menu_label = "compact"
        _menu_started_at(state, ev.session_id or "")
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
        else:
            try:
                n = int(payload.get("compacted_n", 0) or 0)
            except (TypeError, ValueError):
                n = 0
            summary = str(payload.get("summary", "") or "")
            finalize_compact(
                state.compact, compacted_n=n, summary_text=summary)
        # 菜单段 "compact" 计时落点。
        if state.footer.menu_label == "compact":
            state.footer.menu_elapsed_s = _menu_elapsed(state, ev.session_id or "")
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
        label = _parse_menu_label(str(text or ""))
        if label:
            state.footer.menu_label = label
            _menu_started_at(state, ev.session_id or "")
        return
    if kind == "menu_response":
        specs_raw = payload.get("specs") or ()
        specs = tuple(
            (str(name), str(desc)) for name, desc in specs_raw
        )
        built = build_help_menu(card_id=state.card_id, specs=specs)
        state.menu = built.menu
        state.bottom_buttons = built.bottom_buttons
        state.footer.state = "menu"
        state.footer.menu_label = "help"
        return
    if kind == "attach_options":
        cands_raw = payload.get("candidates") or ()
        cands = tuple(
            (str(run_id), str(label)) for run_id, label in cands_raw
        )
        built = build_attach_menu(card_id=state.card_id, candidates=cands)
        state.menu = built.menu
        state.bottom_buttons = built.bottom_buttons
        state.footer.state = "menu"
        state.footer.menu_label = "attach"
        return


# --- helpers --------------------------------------------------------------


def _set_tool_live(sec: AnswerSection, call_id: str, text: str) -> None:
    for coll in ("tools", "_pending_tools_buf"):
        items = list(getattr(sec, coll))
        for i, t in enumerate(items):
            if t.call_id == call_id and t.status == "running":
                items[i] = replace(t, live=text)
                setattr(sec, coll, tuple(items))
                return


def _mark_running_tools_cancelled(sec: AnswerSection) -> None:
    """把所有还没拿到 body 的 tool 改成 🚫(用 run cancel 兜底)。"""
    now = time.monotonic()
    for coll in ("tools", "_pending_tools_buf"):
        items = list(getattr(sec, coll))
        changed = False
        for i, t in enumerate(items):
            if t.body_md:
                continue
            elapsed = now - t.started_at if t.started_at else 0.0
            new_header = _make_tool_header(
                t.name, t.summary, elapsed, "cancel")
            items[i] = replace(t, header=new_header, ok=False, status="cancel",
                               elapsed_s=elapsed, live="")
            changed = True
        if changed:
            setattr(sec, coll, tuple(items))


_SUMMARY_MAX = 30  # 移动端 ~40 字/行,留出 emoji+name+elapsed 余量,保单行


def _trim_path_like(s: str, cap: int) -> str:
    """长字符串截前半段:`/home/xzp/project/long/path/file.py` → `…/long/path/file.py`。
    优先保后部(用户更关心末段标识)。无 `/` 时退回末段截断。"""
    if len(s) <= cap:
        return s
    if "/" in s:
        keep = cap - 2  # 给 "…/" 留 2 位
        tail = s[-keep:]
        # 截到最近的 `/` 边界,避免砍在路径段中间
        i = tail.find("/")
        if 0 < i < keep - 4:
            tail = tail[i:]
        return "…" + tail
    # 无路径分隔符 → 普通末段省略
    return s[:cap - 1] + "…"


def _tool_summary(args: dict[str, Any], summary_args: tuple[str, ...] = ()) -> str:
    """工具标题摘要(< 56 字)。
    工具声明了 summary_args(ToolDisplay)时按它的顺序取第一个非空参数(列表用 " / " 连接);
    否则:description(prompt 让模型给的一句话) > 关键路径/命令字段 > 第一个 kv。
    路径长时从前半截断保末段。"""
    for key in summary_args:
        value = args.get(key)
        if isinstance(value, (list, tuple)):
            value = " / ".join(str(v) for v in value if v)
        if value:
            text = " ".join(str(value).split())
            return text if len(text) <= _SUMMARY_MAX else _trim_path_like(text, _SUMMARY_MAX) \
                if "/" in text and " " not in text else text[:_SUMMARY_MAX - 1] + "…"
    # 1) description(bash/edit 等 prompt 里要求模型给的一行简述)
    desc = str(args.get("description") or "").strip()
    if desc:
        desc = " ".join(desc.split())
        if len(desc) > _SUMMARY_MAX:
            desc = desc[:_SUMMARY_MAX - 1] + "…"
        return desc
    # 2) 路径/命令字段
    s = ""
    for key in ("path", "file_path", "file", "cmd", "command",
                "url", "query", "pattern"):
        if key in args and args[key]:
            s = str(args[key])
            break
    if not s and args:
        # 跳过已读过的 description 取第一个 kv
        for k, v in args.items():
            if k != "description" and v:
                s = f"{k}={v}"
                break
    s = " ".join(s.split())
    return _trim_path_like(s, _SUMMARY_MAX)


def _fmt_tool_elapsed(secs: float) -> str:
    """< 1s → Xms;1–60s → Xs / X.Ys;> 60s → XmYs。"""
    if secs < 0:
        secs = 0.0
    if secs < 1.0:
        return f"{int(secs * 1000)}ms"
    if secs < 60.0:
        return f"{secs:.1f}s" if secs < 10 else f"{int(secs)}s"
    m, s = divmod(int(secs), 60)
    return f"{m}m{s}s"


def _make_tool_header(name: str, summary: str,
                      elapsed_s: float | None,
                      status: str = "") -> str:
    """组合: ``<name>[ · <summary>][ · <elapsed>][ · <status>]``。
    status ∈ {"", "ok", "fail", "cancel"};运行中传空串。"""
    head = f"✎ {name or '?'}"
    if summary:
        head += f" · {summary}"
    if elapsed_s is not None:
        head += f" · {_fmt_tool_elapsed(elapsed_s)}"
    if status:
        head += f" · {status}"
    return head
