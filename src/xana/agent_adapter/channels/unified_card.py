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
