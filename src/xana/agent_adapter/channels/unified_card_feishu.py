"""飞书 renderer: UnifiedCardState -> 飞书 interactive card schema dict。

独立模块,不导入 feishu_card.py;输出可直接喂 lark send_message / patch_message。
Section 顺序固定:top buttons -> compact -> answer -> menu -> bottom buttons -> footer。
"""
from __future__ import annotations

from xana.agent_adapter.channels.unified_card import (
    AnswerSection,
    Button,
    ButtonRow,
    CompactSection,
    TOOLS_VISIBLE_LIMIT,
    UnifiedCardState,
)


def _progress_bar(cells: int) -> str:
    cells = max(0, min(10, cells))
    return "▰" * cells + "▱" * (10 - cells)


def _md(text: str) -> dict:
    return {"tag": "markdown", "content": text}


def _panel(title: str, body_md: str, *, folded: bool = True) -> dict:
    return {
        "tag": "collapsible_panel",
        "expanded": not folded,
        "header": {"title": {"tag": "markdown", "content": title}},
        "elements": [_md(body_md or " ")],
    }


def _btn(b: Button) -> dict:
    out: dict = {
        "tag": "button",
        "type": "default",
        "text": {"tag": "plain_text", "content": b.text},
    }
    if b.send:
        out["value"] = {"send": b.send}
    return out


def _btn_row(row: ButtonRow) -> dict:
    return {
        "tag": "column_set",
        "columns": [
            {"tag": "column", "elements": [_btn(b)]}
            for b in row.buttons
        ],
    }


def _render_compact(sec: CompactSection) -> list[dict]:
    out: list[dict] = []
    if sec.state == "completed":
        title = "上下文压缩完成"
    elif sec.state == "cancelled":
        title = "压缩已取消"
    else:
        title = "正在压缩上下文"
    out.append(_md(f"**{title}**"))
    if sec.state != "cancelled":
        out.append(_md(f"`{_progress_bar(sec.progress_cells)}`"))
    if sec.state == "completed":
        panel_title = f"已压缩 {sec.compacted_n} 条消息"
    elif sec.state == "cancelled":
        panel_title = "压缩已取消"
    else:
        panel_title = "压缩中"
    out.append(_panel(panel_title, sec.summary_text or " ", folded=True))
    return out


def _render_answer(sec: AnswerSection) -> list[dict]:
    out: list[dict] = []
    # thinking
    if sec.thinking_text:
        think_title = "思考过程"
    else:
        think_title = "正在思考"
    out.append(
        _panel(
            think_title,
            sec.thinking_text or " ",
            folded=(sec.state != "streaming"),
        )
    )
    # tools — cap visible to first TOOLS_VISIBLE_LIMIT
    visible_tools = sec.tools[:TOOLS_VISIBLE_LIMIT]
    for t in visible_tools:
        out.append(_panel(t.header, t.body_md or " ", folded=True))
    # tools hint / pending
    if sec.tools_pending > 0:
        out.append(_md(f"**+{sec.tools_pending} 待加载工具**"))
    elif sec.state == "streaming" and not sec.tools and not sec.answer_text:
        out.append(_md("_正在组织回答_"))
    # answer block
    if sec.state == "detached":
        out.append(
            _panel(
                "回答中…",
                "_回答仍在后台生成,完成后会自动更新_",
                folded=True,
            )
        )
    elif sec.state == "terminal":
        out.append(_panel("回答", sec.answer_text or " ", folded=False))
    elif sec.state == "cancelled":
        out.append(_md(f"**回答已取消**\n\n{sec.answer_text}"))
    else:
        out.append(_md(f"**回答**\n\n{sec.answer_text or '_…_'}"))
    return out


def render(state: UnifiedCardState) -> dict:
    """Render a UnifiedCardState to a feishu interactive card message dict."""
    elements: list[dict] = []
    for row in state.top_buttons:
        elements.append(_btn_row(row))
    if state.compact is not None:
        elements.extend(_render_compact(state.compact))
    if state.answer is not None:
        elements.extend(_render_answer(state.answer))
    if state.menu is not None:
        elements.append(_md(state.menu.body_md))
    for row in state.bottom_buttons:
        elements.append(_btn_row(row))
    elements.append(_md(f"_{state.footer.render()}_"))
    return {"msg_type": "interactive", "card": {"elements": elements}}
