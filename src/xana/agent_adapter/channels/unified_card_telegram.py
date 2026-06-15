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


def _render_answer(sec: AnswerSection, *, extra_kb: list[list[dict]],
                   card_id: str) -> str:
    """渲染回答段。tools-overflow 按钮通过 ``extra_kb`` 追加(TG 不能 mid-text)。"""
    parts: list[str] = []
    # thinking — TG <blockquote expandable> 默认折叠,这里就保持一致
    think_title = "思考过程" if sec.thinking_text else "思考中"
    parts.append(_fold(think_title, sec.thinking_text or " "))
    # tools — 全渲染。buf 在按钮里。
    for t in sec.tools:
        parts.append(_fold(t.header, t.body_md or " "))
    if sec.tools_pending > 0:
        if sec.state == "detached":
            parts.append("<b>正在后台调用工具</b>")
        elif sec.state == "terminal":
            label = f"显示剩余 {sec.tools_pending} 工具"
            parts.append(f"<b>{label}</b>")
            extra_kb.append([{
                "text": label,
                "callback_data": f"__load_tools__:{card_id}"[
                    :_TG_CALLBACK_DATA_LIMIT],
            }])
        elif sec.state == "streaming":
            label = f"+{sec.tools_pending} 待加载工具"
            parts.append(f"<b>{label}</b>")
            extra_kb.append([{
                "text": label,
                "callback_data": f"__load_tools__:{card_id}"[
                    :_TG_CALLBACK_DATA_LIMIT],
            }])
    elif sec.state == "streaming" and not sec.tools and not sec.answer_text:
        parts.append("<i>正在组织回答</i>")
    # 回答块:所有状态都用 <blockquote expandable>,保 detach 高度不变(spec §4.2)。
    if sec.state == "detached":
        parts.append(_fold("回答中…", "<i>回答仍在后台生成</i>"))
    elif sec.state == "terminal":
        parts.append(_fold("回答", sec.answer_text or " "))
    elif sec.state == "cancelled":
        parts.append(_fold("回答已取消", sec.answer_text or " "))
    elif sec.answer_text:
        parts.append(_fold("回答", sec.answer_text))
    else:
        parts.append(_fold("正在回答", "<i>…</i>"))
    return "\n".join(parts)


_TG_CALLBACK_DATA_LIMIT = 64


def _kb_row(row: ButtonRow, *, ph_run_id: str | None) -> list[dict]:
    out: list[dict] = []
    for b in row.buttons:
        item: dict = {"text": b.text}
        if b.send:
            item["callback_data"] = b.send[:_TG_CALLBACK_DATA_LIMIT]
        elif b.action == "load_tools":
            item["callback_data"] = f"__load_tools__:{b.action_arg or ''}"[
                :_TG_CALLBACK_DATA_LIMIT]
        elif b.action == "detach":
            arg = b.action_arg or ph_run_id or ""
            item["callback_data"] = f"__detach__:{arg}"[
                :_TG_CALLBACK_DATA_LIMIT]
        else:
            # 兜底:历史 send=None 默认 detach。
            item["callback_data"] = f"__detach__:{ph_run_id or ''}"[
                :_TG_CALLBACK_DATA_LIMIT]
        out.append(item)
    return out


def render(state: UnifiedCardState) -> tuple[str, list[list[dict]]]:
    """Render a UnifiedCardState to (html_text, inline_keyboard rows)."""
    parts: list[str] = []
    extra_kb: list[list[dict]] = []
    if state.compact is not None:
        parts.append(_render_compact(state.compact))
    if state.answer is not None:
        parts.append(_render_answer(
            state.answer, extra_kb=extra_kb, card_id=state.card_id))
    if state.menu is not None:
        parts.append(state.menu.body_md)
    parts.append(f"<i>{state.footer.render()}</i>")
    html = "\n\n".join(parts)

    kb: list[list[dict]] = []
    for row in state.top_buttons:
        kb.append(_kb_row(row, ph_run_id=state.ph_run_id))
    kb.extend(extra_kb)
    for row in state.bottom_buttons:
        kb.append(_kb_row(row, ph_run_id=state.ph_run_id))
    return html, kb
