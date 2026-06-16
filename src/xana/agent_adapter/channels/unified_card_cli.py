"""CLI renderer: UnifiedCardState -> Textual widget tree.

对齐 feishu/tg 的第三个 View。返回一组 Textual widget,由 XanacApp
(或任何宿主)负责挂到容器里。

设计:
  - compact:进度条 Static + Collapsible(标题=已压缩 N 条消息)
  - thinking:Collapsible(标题=思考过程,默认折叠)
  - tools(visible ≤3):每个一个 Collapsible
  - 隐藏 tools:`+N 工具调用` 按钮(action=load_tools)
  - answer:Static(Rich markup,不折叠)
  - menu:Markdown
  - top/bottom buttons:Horizontal of CardButton(自带 spec dataclass,
    宿主监听 Button.Pressed → button.spec 路由)
  - footer:Static(dim)

每次 render 返回全新一组 widget,宿主用 remove_children + mount 替换。
"""
from __future__ import annotations

from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Markdown, Static

from xana.agent_adapter.channels.unified_card import (
    AnswerSection,
    Button as CardButtonSpec,
    ButtonRow,
    CompactSection,
    UnifiedCardState,
)


_BUDGET_THINKING = 4000
_BUDGET_TOOL_BODY = 2000
_BUDGET_ANSWER = 12000
_VISIBLE_TOOLS = 3


class CardButton(Button):
    """带 spec 字段的 Button,宿主在 on_button_pressed 里通过
    `event.button.spec` 拿到原始 Button dataclass(send / action /
    action_arg),然后自己路由。"""

    def __init__(self, spec: CardButtonSpec, *, ph_run_id: str | None = None,
                 card_id: str = "") -> None:
        super().__init__(spec.text or "·")
        self.spec = spec
        self.ph_run_id = ph_run_id
        self.card_id = card_id


def _progress_bar(cells: int) -> str:
    cells = max(0, min(10, cells))
    return "▰" * cells + "▱" * (10 - cells)


def _trunc(s: str, cap: int, suffix: str = "\n…(已折叠)") -> str:
    if len(s) <= cap:
        return s
    return s[:cap].rstrip() + suffix


def _render_compact(sec: CompactSection) -> list[Widget]:
    if sec.state == "completed":
        status = "完成压缩"
        panel_title = f"已压缩 {sec.compacted_n} 条消息"
    elif sec.state == "cancelled":
        status = "压缩已取消"
        panel_title = "压缩已取消"
    else:
        status = "压缩中"
        panel_title = "压缩中"
    head = (
        status if sec.state == "cancelled"
        else f"{_progress_bar(sec.progress_cells)} {status}"
    )
    body = (sec.summary_text or " ").rstrip() or "(空)"
    return [
        Static(head, classes="card-compact-head"),
        Collapsible(Static(body), title=panel_title, collapsed=True),
    ]


def _render_answer(sec: AnswerSection) -> list[Widget]:
    out: list[Widget] = []

    if sec.thinking_text.strip():
        body = _trunc(sec.thinking_text, _BUDGET_THINKING).rstrip() or "(空)"
        out.append(Collapsible(
            Static(body),
            title="❋ 思考过程" if sec.thinking_text else "❋ 思考中",
            collapsed=True,
        ))

    visible_n = min(_VISIBLE_TOOLS, len(sec.tools))
    for t in sec.tools[:visible_n]:
        body = _trunc(t.body_md or " ", _BUDGET_TOOL_BODY).rstrip() or "(空)"
        out.append(Collapsible(
            Static(body), title=t.header, collapsed=True,
        ))
    hidden = (len(sec.tools) - visible_n) + sec.tools_pending
    if hidden > 0:
        # 用 Button 形式触发 action=load_tools,与 tg / feishu 一致。
        spec = CardButtonSpec(
            text=f"展开 ✎×{hidden}",
            action="load_tools",
        )
        out.append(Horizontal(
            CardButton(spec), classes="card-btn-row",
        ))

    if sec.state == "cancelled":
        title = "✦ 回答已取消"
    else:
        title = "✦ 回答" if sec.answer_text else "✦ 回答中"
    body = _trunc(sec.answer_text or " ", _BUDGET_ANSWER)
    out.append(Static(
        f"[bold]{title}[/bold]\n{body}",
        markup=True, classes="card-answer",
    ))
    return out


def _render_btn_row(
    rows: tuple[ButtonRow, ...], *, ph_run_id: str | None, card_id: str,
) -> list[Widget]:
    out: list[Widget] = []
    for row in rows:
        if not row.buttons:
            continue
        btns = [
            CardButton(b, ph_run_id=ph_run_id, card_id=card_id)
            for b in row.buttons if b.text
        ]
        if btns:
            out.append(Horizontal(*btns, classes="card-btn-row"))
    return out


def render(state: UnifiedCardState) -> list[Widget]:
    """UnifiedCardState → Textual widget list(顶到底顺序)。"""
    out: list[Widget] = []

    if state.compact is not None:
        out.extend(_render_compact(state.compact))
    if state.answer is not None:
        out.extend(_render_answer(state.answer))
    if state.menu is not None and state.menu.body_md:
        out.append(Markdown(state.menu.body_md))

    out.extend(_render_btn_row(
        state.top_buttons, ph_run_id=state.ph_run_id, card_id=state.card_id))
    out.extend(_render_btn_row(
        state.bottom_buttons, ph_run_id=state.ph_run_id, card_id=state.card_id))

    footer_text = state.footer.render()
    if footer_text:
        out.append(Static(f"[dim italic]{footer_text}[/dim italic]",
                          markup=True, classes="card-footer"))
    return out
