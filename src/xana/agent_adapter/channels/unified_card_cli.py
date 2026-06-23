"""Terminal (string) renderer for UnifiedCardState.

This is the oneshot-friendly view used by xanac when it prints once
after a turn reaches a terminal state. It returns a plain-text string;
no chrome, no colours, no widgets — just the sections in their natural
order.

For the Textual interactive view, see the widget-based renderer used by
XanacApp (kept under a different module name to avoid clashing).
"""
from __future__ import annotations

from xana.agent_adapter.channels.unified_card import (
    AnswerSection,
    CompactSection,
    UnifiedCardState,
)


_BUDGET_THINKING = 4000
_BUDGET_TOOL_BODY = 2000
_BUDGET_ANSWER = 12000
_VISIBLE_TOOLS = 3


def _trunc(s: str, cap: int, suffix: str = "\n…(已折叠)") -> str:
    if len(s) <= cap:
        return s
    return s[:cap].rstrip() + suffix


def _render_compact(sec: CompactSection) -> str:
    if sec.state == "completed":
        head = f"[compact] 完成压缩 · 共 {sec.compacted_n} 条"
    elif sec.state == "cancelled":
        head = "[compact] 压缩已取消"
    else:
        head = "[compact] 压缩中"
    body = (sec.summary_text or "").rstrip()
    if body:
        return f"{head}\n{body}"
    return head


def _render_answer(sec: AnswerSection) -> str:
    parts: list[str] = []
    if sec.thinking_text.strip():
        body = _trunc(sec.thinking_text, _BUDGET_THINKING).rstrip()
        parts.append(f"❋ 思考过程\n{body}")

    visible_n = min(_VISIBLE_TOOLS, len(sec.tools))
    for t in sec.tools[:visible_n]:
        body = _trunc(t.body_md or "", _BUDGET_TOOL_BODY).rstrip()
        if body:
            parts.append(f"{t.header}\n{body}")
        else:
            parts.append(t.header)
    hidden = (len(sec.tools) - visible_n) + sec.tools_pending
    if hidden > 0:
        parts.append(f"(+{hidden} 工具调用未展开)")

    if sec.state == "cancelled":
        title = "✦ 回答已取消"
    else:
        title = "✦ 回答" if sec.answer_text else "✦ 回答中"
    body = _trunc(sec.answer_text or "", _BUDGET_ANSWER).rstrip()
    if body:
        parts.append(f"{title}\n{body}")
    else:
        parts.append(title)
    return "\n\n".join(parts)


def render(state: UnifiedCardState) -> str:
    """UnifiedCardState → plain-text string (terminal-friendly)."""
    parts: list[str] = []
    if state.compact is not None:
        parts.append(_render_compact(state.compact))
    if state.answer is not None:
        parts.append(_render_answer(state.answer))
    if state.menu is not None and state.menu.body_md:
        parts.append(state.menu.body_md.rstrip())
    footer_text = state.footer.render()
    if footer_text:
        parts.append(footer_text)
    return "\n\n".join(p for p in parts if p)


# --- Textual widget tree renderer (interactive xanac) --------------------


def _w_trunc(s: str, cap: int, suffix: str = "\n…(已折叠)") -> str:
    if len(s) <= cap:
        return s
    return s[:cap].rstrip() + suffix


def _make_card_button_cls():
    """Lazy module-level CardButton factory (cached on first call).

    Defined at module scope so `isinstance(btn, CardButton)` from
    external callers stays stable across renders. (Previously this was
    a nested class inside render_widgets — every call produced a fresh
    type object, breaking isinstance checks.)
    """
    cls = globals().get("_CardButton")
    if cls is not None:
        return cls
    from textual.widgets import Button
    from xana.agent_adapter.channels.unified_card import (
        Button as CardButtonSpec,
    )

    class CardButton(Button):
        def __init__(self, spec: CardButtonSpec, *, ph_run_id: str | None = None,
                     card_id: str = "") -> None:
            super().__init__(spec.text or "·")
            self.spec = spec
            self.ph_run_id = ph_run_id
            self.card_id = card_id

    globals()["_CardButton"] = CardButton
    return CardButton


def render_widgets(state: UnifiedCardState):  # type: ignore[no-untyped-def]
    """UnifiedCardState → list[Widget] (Textual interactive view).

    Imported lazily so non-Textual oneshot path doesn't pay the import cost.
    """
    from textual.containers import Horizontal
    from textual.widget import Widget
    from textual.widgets import Collapsible, Markdown, Static

    from xana.agent_adapter.channels.unified_card import (
        AnswerSection as _Answer,
        Button as CardButtonSpec,
        ButtonRow,
        CompactSection as _Compact,
    )

    CardButton = _make_card_button_cls()

    def _w_compact(sec: _Compact) -> list[Widget]:
        if sec.state == "completed":
            status = "完成压缩"
            panel_title = f"已压缩 {sec.compacted_n} 条消息"
        elif sec.state == "cancelled":
            status = "压缩已取消"
            panel_title = "压缩已取消"
        else:
            status = "压缩中"
            panel_title = "压缩中"
        cells = max(0, min(10, sec.progress_cells))
        bar = "▰" * cells + "▱" * (10 - cells)
        head = status if sec.state == "cancelled" else f"{bar} {status}"
        body = (sec.summary_text or " ").rstrip() or "(空)"
        return [
            Static(head, classes="card-compact-head"),
            Collapsible(Static(body), title=panel_title, collapsed=True),
        ]

    def _w_answer(sec: _Answer) -> list[Widget]:
        out: list[Widget] = []
        if sec.thinking_text.strip():
            body = _w_trunc(sec.thinking_text, _BUDGET_THINKING).rstrip() or "(空)"
            out.append(Collapsible(
                Static(body),
                title="❋ 思考过程" if sec.thinking_text else "❋ 思考中",
                collapsed=True,
            ))
        visible_n = min(_VISIBLE_TOOLS, len(sec.tools))
        for t in sec.tools[:visible_n]:
            body = _w_trunc(t.body_md or " ", _BUDGET_TOOL_BODY).rstrip() or "(空)"
            out.append(Collapsible(Static(body), title=t.header, collapsed=True))
        hidden = (len(sec.tools) - visible_n) + sec.tools_pending
        if hidden > 0:
            spec = CardButtonSpec(text=f"展开 ✎×{hidden}", action="load_tools")
            out.append(Horizontal(
                CardButton(spec, ph_run_id=state.ph_run_id, card_id=state.card_id),
                classes="card-btn-row",
            ))
        if sec.state == "cancelled":
            title = "✦ 回答已取消"
        else:
            title = "✦ 回答" if sec.answer_text else "✦ 回答中"
        body = _w_trunc(sec.answer_text or " ", _BUDGET_ANSWER)
        out.append(Static(
            f"[bold]{title}[/bold]\n{body}",
            markup=True, classes="card-answer",
        ))
        return out

    def _w_btn_rows(rows: tuple[ButtonRow, ...]) -> list[Widget]:
        out: list[Widget] = []
        for row in rows:
            if not row.buttons:
                continue
            btns = [
                CardButton(b, ph_run_id=state.ph_run_id, card_id=state.card_id)
                for b in row.buttons if b.text
            ]
            if btns:
                out.append(Horizontal(*btns, classes="card-btn-row"))
        return out

    out: list[Widget] = []
    if state.compact is not None:
        out.extend(_w_compact(state.compact))
    if state.answer is not None:
        out.extend(_w_answer(state.answer))
    if state.menu is not None and state.menu.body_md:
        out.append(Markdown(state.menu.body_md))
    out.extend(_w_btn_rows(state.top_buttons))
    out.extend(_w_btn_rows(state.bottom_buttons))
    footer = state.footer.render()
    if footer:
        out.append(Static(
            f"[dim italic]{footer}[/dim italic]",
            markup=True, classes="card-footer",
        ))
    return out
