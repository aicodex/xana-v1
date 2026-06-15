"""统一 PH 卡片模型(spec: 2026-06-15-unified-ph-card-design.md)。

Task 1 scope: 平台无关数据模型 dataclasses + 默认值。
状态机 / renderer / 完整 footer 三态见后续 Task。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


CompactState = Literal["input", "streaming", "completed", "cancelled"]
AnswerState = Literal["streaming", "detached", "terminal", "cancelled"]
FooterState = Literal["fg_running", "bg_frozen", "terminal"]


@dataclass
class ToolBlock:
    name: str
    header: str                  # 例 "Read /foo/bar.py"
    body_md: str
    ok: bool = True


@dataclass
class CompactSection:
    state: CompactState = "input"
    progress_cells: int = 0       # 0..10
    compacted_n: int = 0
    summary_text: str = ""


@dataclass
class AnswerSection:
    state: AnswerState = "streaming"
    thinking_text: str = ""
    tools: tuple[ToolBlock, ...] = ()
    tools_pending: int = 0        # 待加载工具数 (累积 - 已显示)
    answer_text: str = ""
    _pending_tools_buf: tuple[ToolBlock, ...] = ()


@dataclass
class MenuSection:
    body_md: str = ""             # /help 等纯命令响应内容


@dataclass
class Button:
    text: str
    send: str | None = None       # 点击发送的命令


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
    model: str = ""
    ctx_used: int = 0
    ctx_max: int = 0
    elapsed_s: int = 0
    state: FooterState = "fg_running"

    def render(self) -> str:
        """Render footer line per state.

        - fg_running / terminal: ``model · 上下文 used/max · 用时 T``
        - bg_frozen: ``model · 🟡 后台运行中 · 用时 T`` (frozen ctx hidden)
        """
        t = _format_elapsed(self.elapsed_s)
        if self.state == "bg_frozen":
            return f"{self.model} · 🟡 后台运行中 · 用时 {t}"
        return f"{self.model} · 上下文 {self.ctx_used}/{self.ctx_max} · 用时 {t}"


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
    """Reveal all buffered tools into ``sec.tools``; reset pending count."""
    if sec._pending_tools_buf:
        sec.tools = sec.tools + sec._pending_tools_buf
        sec._pending_tools_buf = ()
    sec.tools_pending = 0


def detach_answer(sec: AnswerSection) -> None:
    """Flip to detached state; content preserved (renderer collapses)."""
    sec.state = "detached"


def finalize_answer(sec: AnswerSection, *, final_text: str) -> None:
    """Terminal transition; replace answer_text with final."""
    sec.state = "terminal"
    sec.answer_text = final_text


def cancel_answer(sec: AnswerSection) -> None:
    """Cancel; subsequent append_* are no-ops."""
    sec.state = "cancelled"


# --- top buttons + detach / ph_replaced transitions ------------------------


def compute_top_buttons(state: "UnifiedCardState") -> tuple[ButtonRow, ...]:
    """Top button rules per spec §5.

    - detached / terminal: no buttons.
    - answer.streaming (regardless of compact): ⏹ 停止 / ➡ 后台.
      compact cancel is suppressed because cancelling re-wakes the run.
    - compact.input|streaming alone (no answer running): single 取消.
    - otherwise: none.
    """
    if state.detached or state.terminal:
        return ()
    a_running = state.answer is not None and state.answer.state == "streaming"
    c_running = (
        state.compact is not None
        and state.compact.state in ("input", "streaming")
    )
    if a_running:
        run_id = state.ph_run_id or ""
        return (ButtonRow(buttons=(
            Button(text="⏹ 停止", send=f"/cancel {run_id}".strip()),
            Button(text="➡ 后台", send=None),
        )),)
    if c_running:
        run_id = state.ph_run_id or ""
        return (ButtonRow(buttons=(
            Button(text="取消", send=f"/compact_cancel {run_id}".strip()),
        )),)
    return ()


def apply_detach(state: "UnifiedCardState") -> None:
    """Detach transition: clear top buttons, freeze footer, mark answer detached.

    Content preserved; renderer collapses based on state. Compact section
    untouched (a parallel compact run continues to update independently).
    """
    state.detached = True
    if state.answer is not None:
        detach_answer(state.answer)
    state.footer.state = "bg_frozen"
    state.top_buttons = ()


def apply_ph_replaced(
    state: "UnifiedCardState",
    *,
    final_answer: str,
    ctx_used: int,
    elapsed_s: int,
) -> None:
    """Terminal transition triggered by ph_replaced.

    Replaces answer with final text, restores ctx in footer, clears buttons.
    Compact section is left as-is (if completed it stays completed).
    """
    state.terminal = True
    state.detached = False
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
