"""统一 PH 卡片模型(spec: 2026-06-15-unified-ph-card-design.md)。

Task 1 scope: 平台无关数据模型 dataclasses + 默认值。
状态机 / renderer / 完整 footer 三态见后续 Task。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


CompactState = Literal["input", "streaming", "completed", "cancelled"]
AnswerState = Literal["streaming", "detached", "terminal", "cancelled"]
FooterState = Literal["fg_running", "bg_frozen", "terminal", "menu"]
ExpandMode = Literal["default", "thinking", "tools", "answer"]


def _fmt_k(n: int) -> str:
    """Format an int as k-rounded short form. <1000 stays as-is."""
    if n < 1000:
        return str(n)
    return f"{n // 1000}k"


def _fmt_short_time(secs: float) -> str:
    """Format short elapsed for menu/command segments. <1s → '0.Ns'."""
    if secs < 0:
        secs = 0.0
    if secs < 1:
        return f"{secs:.1f}s"
    s = int(secs)
    if s < 60:
        return f"{s}s"
    m, r = divmod(s, 60)
    if m < 60:
        return f"{m}m {r}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {r}s"


@dataclass
class ToolBlock:
    name: str
    header: str                  # 例 "🛠 read /foo/bar.py"
    body_md: str
    ok: bool = True
    call_id: str = ""            # 对应 harness tool_started/finished 的 call_id
    started_at: float = 0.0      # monotonic 起点,用于算 elapsed
    summary: str = ""            # 短摘要(图标无关,生成 header 时拼)


@dataclass
class CompactSection:
    state: CompactState = "input"
    progress_cells: int = 0       # 0..10
    compacted_n: int = 0
    summary_text: str = ""


@dataclass
class AnswerSection:
    """回答段。tools 是已渲染面板,_pending_tools_buf 是按钮后面的隐藏 tool。

    - 新 tool 到来:len(tools)<3 → tools;否则 → buf。
    - 用户点按钮(adapter 调 load_pending_tools): buf → tools, buf 清空。
    - 之后又来新 tool: 又进 buf, 按钮又出现, 标签反映新 buf 数。
    - tools_pending 与 len(buf) 同步,renderer 据此显示/隐藏按钮。
    """
    state: AnswerState = "streaming"
    thinking_text: str = ""
    tools: tuple[ToolBlock, ...] = ()
    tools_pending: int = 0                    # = len(_pending_tools_buf)
    answer_text: str = ""
    page: int = 0                              # 答案翻页用(TG 单消息长度有限)
    _pending_tools_buf: tuple[ToolBlock, ...] = ()


@dataclass
class MenuSection:
    body_md: str = ""             # /help 等纯命令响应内容


@dataclass
class Button:
    """卡片按钮。

    三种点击形态(互斥):
    - ``send``:点击 = 用户发了一条命令文本(走 router,会回话)
    - ``action`` + ``action_arg``:adapter 本地动作(detach / load_tools 等),
      不走 router、不回话,只刷卡片
    - 两者都空:占位按钮(静默,不响应)
    """
    text: str
    send: str | None = None
    action: str | None = None
    action_arg: str | None = None


@dataclass
class ButtonRow:
    buttons: tuple[Button, ...] = ()


def _format_elapsed(secs: int) -> str:
    """Format elapsed seconds as Xh Ym Zs / Xm Ys / Zs.

    Skip leading zero units; once an hour appears all three units appear,
    once a minute appears both m and s appear. Negative input clamps to 0.
    """
    if secs < 0:
        secs = 0
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


@dataclass
class FooterText:
    """分段脚标,四种形态(规格 session-and-card-state.md §5.6):

        fg_running:  🟡 model · 上下文 Nk/Mk · 回答用时 T
        terminal:    🟢/🔴 model · 上下文 Nk/Mk · 回答用时 T
        bg_frozen:   🟡 正在后台回答 [· {label} 用时 t]
        menu:        {label} 用时 t

    - model + 上下文 + 回答用时 三件套绑定,缺一不渲染(I9)。
    - fg_running / terminal 绝不带菜单段;菜单段只出现在 menu 与 bg_frozen(排尾)。
    """
    model: str = ""
    ctx_used: int = 0
    ctx_max: int = 0
    elapsed_s: int = 0                # 回答(主 turn)用时
    state: FooterState = "fg_running"
    menu_label: str = ""              # 例 "compact" / "clear" / "cancel"
    menu_elapsed_s: float = 0.0       # 菜单/命令用时
    cancelled: bool = False           # 用户已取消 → 终态显示 🔴 而非 🟢

    def render(self) -> str:
        """按 state 渲染四种形态之一,见类文档。"""
        parts: list[str] = []
        # 1) menu 卡: {label} 用时 T (无 dot, 无 ctx, 无回答用时)
        if self.state == "menu":
            if self.menu_label:
                parts.append(
                    f"{self.menu_label} 用时 "
                    f"{_fmt_short_time(self.menu_elapsed_s)}"
                )
            return " · ".join(parts)
        # 2) bg_frozen: 🟡 正在后台回答 [· compact 用时 T]
        if self.state == "bg_frozen":
            parts.append("🟡 正在后台回答")
            if self.menu_label:
                parts.append(
                    f"{self.menu_label} 用时 "
                    f"{_fmt_short_time(self.menu_elapsed_s)}"
                )
            return " · ".join(parts)
        # 3) fg_running / 4) terminal: 🟡/🟢 model · 上下文 · 回答用时
        # **三者绑定出现**:任何一个还没就绪就不渲染 footer(避免出现裸的
        # "🟡 · 回答用时 0s")。**不带 menu 段**。
        if not self.model or self.ctx_max <= 0:
            return ""
        if self.cancelled:
            dot = "🔴"
        elif self.state == "terminal":
            dot = "🟢"
        else:
            dot = "🟡"
        parts.append(f"{dot} {self.model}")
        parts.append(
            f"上下文 {_fmt_k(self.ctx_used)}/{_fmt_k(self.ctx_max)}"
        )
        parts.append(f"回答用时 {_format_elapsed(self.elapsed_s)}")
        return " · ".join(parts)


# --- compact section state machine ---------------------------------------

SUMMARY_SECTIONS = 7
_COMPACT_INPUT_ESTIMATE_S = 20.0


def advance_compact_input(sec: CompactSection, elapsed_s: float) -> None:
    """input 阶段按时间推进 0→7 格。20s 估算总长。

    streaming/completed/cancelled 状态为 no-op。
    """
    if sec.state != "input":
        return
    if elapsed_s < 0:
        elapsed_s = 0.0
    sec.progress_cells = min(7, int(elapsed_s / _COMPACT_INPUT_ESTIMATE_S * 7))


def advance_compact_streaming(sec: CompactSection, sections_done: int) -> None:
    """streaming 阶段从 7 起,SUMMARY_SECTIONS=7,每段 0.43 → floor(N/7*3)。

    首次调用(sections_done=0)即把 state 切到 streaming 并跳到 7 格。
    """
    sec.state = "streaming"
    if sections_done < 0:
        sections_done = 0
    sec.progress_cells = min(10, 7 + int(sections_done / SUMMARY_SECTIONS * 3))


def finalize_compact(
    sec: CompactSection, *, compacted_n: int, summary_text: str
) -> None:
    sec.state = "completed"
    sec.progress_cells = 10
    sec.compacted_n = compacted_n
    sec.summary_text = summary_text


def cancel_compact(sec: CompactSection) -> None:
    sec.state = "cancelled"


# --- answer section state machine + tools overflow ---------------------------

TOOLS_VISIBLE_LIMIT = 3


def append_thinking(sec: AnswerSection, delta: str) -> None:
    """Accumulate thinking text; no-op if cancelled."""
    if sec.state == "cancelled":
        return
    sec.thinking_text += delta


def append_answer(sec: AnswerSection, delta: str) -> None:
    """Accumulate answer text; no-op if cancelled."""
    if sec.state == "cancelled":
        return
    sec.answer_text += delta


def append_tool(sec: AnswerSection, tool: ToolBlock) -> None:
    """Append a tool. First TOOLS_VISIBLE_LIMIT go visible; rest buffer.

    No-op if cancelled.
    """
    if sec.state == "cancelled":
        return
    total = len(sec.tools) + sec.tools_pending
    if total < TOOLS_VISIBLE_LIMIT:
        sec.tools = sec.tools + (tool,)
    else:
        sec.tools_pending += 1
        sec._pending_tools_buf = sec._pending_tools_buf + (tool,)


def load_pending_tools(sec: AnswerSection) -> None:
    """Reveal all buffered tools: buf → tools, buf cleared, pending=0.

    后续如果还有新 tool 到来,会再次进 buf,按钮重新出现。
    """
    if sec._pending_tools_buf:
        sec.tools = sec.tools + sec._pending_tools_buf
        sec._pending_tools_buf = ()
    sec.tools_pending = 0


def detach_answer(sec: AnswerSection) -> None:
    """Flip to detached state; content preserved (renderer collapses)."""
    sec.state = "detached"


def finalize_answer(sec: AnswerSection, *, final_text: str) -> None:
    """Terminal transition; replace answer_text with final.

    If ``final_text`` is empty, preserve whatever has been accumulated
    (后台 run 在 detach 期间通过 model_delta 累积的答案不应被空 ph_replaced 抹掉)。
    若已被 cancel,保留 cancelled 状态 — finalize 不能把"已取消"覆盖成"完成"。
    """
    if sec.state != "cancelled":
        sec.state = "terminal"
    if final_text:
        sec.answer_text = final_text


def cancel_answer(sec: AnswerSection) -> None:
    """Cancel; subsequent append_* are no-ops."""
    sec.state = "cancelled"


# --- top buttons + detach / ph_replaced transitions ------------------------


def compute_top_buttons(state: "UnifiedCardState") -> tuple[ButtonRow, ...]:
    """Top button rules。

    - terminal: 无按钮
    - 前台 + answer.streaming: "取消回答" + "后台回答"
    - 后台(detached) + answer 未终止: "取消回答"(用户可取消后台 run)
    - compact 单独运行: "取消"
    - otherwise: 无
    """
    if state.terminal:
        return ()
    a_alive = (state.answer is not None
               and state.answer.state in ("streaming", "detached"))
    c_running = (
        state.compact is not None
        and state.compact.state in ("input", "streaming")
    )
    run_id = state.ph_run_id or ""
    if a_alive:
        cancel_btn = Button(
            text="取消回答", send=f"/cancel {run_id}".strip())
        if state.detached:
            return (ButtonRow(buttons=(cancel_btn,)),)
        return (ButtonRow(buttons=(
            cancel_btn,
            Button(text="后台回答", send=None),
        )),)
    if c_running:
        return (ButtonRow(buttons=(
            Button(text="取消", send=f"/compact_cancel {run_id}".strip()),
        )),)
    return ()


def apply_detach(state: "UnifiedCardState") -> None:
    """Detach transition: mark answer detached, freeze footer, refresh buttons。
    保留"取消回答"按钮在 bg 卡顶部(用户可取消后台 run)。
    """
    state.detached = True
    if state.answer is not None:
        detach_answer(state.answer)
    state.footer.state = "bg_frozen"
    state.top_buttons = compute_top_buttons(state)


def apply_ph_replaced(
    state: "UnifiedCardState",
    *,
    final_answer: str,
    ctx_used: int,
    elapsed_s: int,
    cancelled: bool = False,
) -> None:
    """Terminal transition triggered by ph_replaced.

    Replaces answer with final text, restores ctx in footer, clears buttons.
    Compact section is left as-is (if completed it stays completed).
    cancelled=True 时 footer 红点 + answer 状态保留 cancelled。
    """
    if state.detached:
        state.terminal_from_bg = True
    state.terminal = True
    state.detached = False
    if cancelled:
        state.footer.cancelled = True
    if state.answer is not None:
        finalize_answer(state.answer, final_text=final_answer)
    state.footer.state = "terminal"
    state.footer.ctx_used = ctx_used
    state.footer.elapsed_s = elapsed_s
    state.top_buttons = ()


# --- menu section builders (Task 6) ----------------------------------------


def build_help_menu(
    *, card_id: str, specs: tuple[tuple[str, str], ...]
) -> "UnifiedCardState":
    """Build a /help-style menu card.

    Body lists ``name  desc`` lines; bottom buttons replay each command,
    laid out two per row.
    """
    lines = ["可用命令:"]
    for name, desc in specs:
        lines.append(f"  {name:<20} {desc}")
    body = "\n".join(lines)
    sends = [Button(text=name, send=name) for name, _ in specs]
    rows = tuple(
        ButtonRow(buttons=tuple(sends[i : i + 2])) for i in range(0, len(sends), 2)
    )
    return UnifiedCardState(
        card_id=card_id,
        menu=MenuSection(body_md=body),
        bottom_buttons=rows,
        footer=FooterText(state="menu"),
    )


def build_attach_menu(
    *, card_id: str, candidates: tuple[tuple[str, str], ...]
) -> "UnifiedCardState":
    """Build an /attach candidate-list menu card.

    Body lists each candidate run_id + label; bottom buttons (one per row)
    send ``/attach <run_id>``.
    """
    lines = ["可恢复的后台 run:"]
    btns: list[Button] = []
    for run_id, label in candidates:
        lines.append(f"  {run_id}  · {label[:30]}")
        btns.append(Button(text=f"切回 {label[:20]}", send=f"/attach {run_id}"))
    rows = tuple(ButtonRow(buttons=(b,)) for b in btns)
    return UnifiedCardState(
        card_id=card_id,
        menu=MenuSection(body_md="\n".join(lines)),
        bottom_buttons=rows,
        footer=FooterText(state="menu"),
    )


@dataclass
class UnifiedCardState:
    card_id: str
    message_id: str | None = None
    ph_run_id: str | None = None
    top_buttons: tuple[ButtonRow, ...] = ()
    compact: CompactSection | None = None
    answer: AnswerSection | None = None
    menu: MenuSection | None = None
    bottom_buttons: tuple[ButtonRow, ...] = ()
    footer: FooterText = field(default_factory=FooterText)
    detached: bool = False
    terminal: bool = False
    terminal_from_bg: bool = False   # 后台回写终态(用于 renderer 默认折叠回答)
    linkify_commands: bool = False   # True = 正文 /cmd 原生可点(TG),renderer 可隐 bottom_buttons
    expand_mode: ExpandMode = "default"   # 全屏 thinking/tools/answer
    expand_page: int = 0                  # expand 模式翻页
