"""Telegram renderer: UnifiedCardState -> (html, inline_keyboard).

Independent module — does not import telegram.py / telegram_html.py.
Section order is identical to the feishu renderer:
  top_buttons -> compact -> answer -> menu -> bottom_buttons -> footer.

TG cannot place buttons mid-message, so all top_buttons + bottom_buttons stack
into the inline_keyboard (top rows first, bottom rows after). Folded blocks are
expressed as <blockquote expandable> ... </blockquote>.
"""
from __future__ import annotations

from xana.agent_adapter.channels.unified_card import (
    AnswerSection,
    ButtonRow,
    CompactSection,
    TOOLS_VISIBLE_LIMIT,
    UnifiedCardState,
)


def _progress_bar(cells: int) -> str:
    cells = max(0, min(10, cells))
    return "▰" * cells + "▱" * (10 - cells)


def _fold(title: str, body: str) -> str:
    body = body or " "
    return f"<blockquote expandable><b>{title}</b>\n{body}</blockquote>"


def _render_compact(sec: CompactSection) -> str:
    if sec.state == "completed":
        title = "上下文压缩完成"
    elif sec.state == "cancelled":
        title = "压缩已取消"
    else:
        title = "正在压缩上下文"
    parts: list[str] = [f"<b>{title}</b>"]
    if sec.state != "cancelled":
        parts.append(f"<code>{_progress_bar(sec.progress_cells)}</code>")
    if sec.state == "completed":
        panel_title = f"已压缩 {sec.compacted_n} 条消息"
    elif sec.state == "cancelled":
        panel_title = "压缩已取消"
    else:
        panel_title = "压缩中"
    parts.append(_fold(panel_title, sec.summary_text or " "))
    return "\n".join(parts)


def _render_answer(sec: AnswerSection) -> str:
    parts: list[str] = []
    # thinking
    think_title = "思考过程" if sec.thinking_text else "正在思考"
    parts.append(_fold(think_title, sec.thinking_text or " "))
    # tools (cap visible)
    visible_tools = sec.tools[:TOOLS_VISIBLE_LIMIT]
    for t in visible_tools:
        parts.append(_fold(t.header, t.body_md or " "))
    # pending hint / streaming placeholder
    if sec.tools_pending > 0:
        parts.append(f"<b>+{sec.tools_pending} 待加载工具</b>")
    elif sec.state == "streaming" and not sec.tools and not sec.answer_text:
        parts.append("<i>正在组织回答</i>")
    # answer body
    if sec.state == "detached":
        parts.append(
            _fold("回答中…", "<i>回答仍在后台生成,完成后会自动更新</i>")
        )
    elif sec.state == "terminal":
        parts.append(_fold("回答", sec.answer_text or " "))
    elif sec.state == "cancelled":
        parts.append(f"<b>回答已取消</b>\n\n{sec.answer_text}")
    else:
        parts.append(f"<b>回答</b>\n\n{sec.answer_text or '<i>…</i>'}")
    return "\n".join(parts)


def _kb_row(row: ButtonRow) -> list[dict]:
    out: list[dict] = []
    for b in row.buttons:
        item: dict = {"text": b.text}
        if b.send:
            item["callback_data"] = b.send
        out.append(item)
    return out


def render(state: UnifiedCardState) -> tuple[str, list[list[dict]]]:
    """Render a UnifiedCardState to (html_text, inline_keyboard rows)."""
    parts: list[str] = []
    if state.compact is not None:
        parts.append(_render_compact(state.compact))
    if state.answer is not None:
        parts.append(_render_answer(state.answer))
    if state.menu is not None:
        parts.append(state.menu.body_md)
    parts.append(f"<i>{state.footer.render()}</i>")
    html = "\n\n".join(parts)

    kb: list[list[dict]] = []
    for row in state.top_buttons:
        kb.append(_kb_row(row))
    for row in state.bottom_buttons:
        kb.append(_kb_row(row))
    return html, kb
