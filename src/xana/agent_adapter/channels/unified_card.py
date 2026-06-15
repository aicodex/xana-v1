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
    """Task 2 owns full coverage; minimal impl here just for fg_running smoke."""
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
        # Task 2 owns bg_frozen / terminal variants; smoke impl for fg_running.
        t = _format_elapsed(self.elapsed_s)
        return f"{self.model} · 上下文 {self.ctx_used}/{self.ctx_max} · 用时 {t}"


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
