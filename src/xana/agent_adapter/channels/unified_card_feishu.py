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


def _btn(b: Button, *, ph_run_id: str | None) -> dict:
    out: dict = {
        "tag": "button",
        "type": "default",
        "text": {"tag": "plain_text", "content": b.text},
    }
    if b.send:
        out["value"] = {"send": b.send}
    elif b.action == "load_tools":
        out["value"] = {"load_tools": b.action_arg or ""}
    elif b.action == "detach":
        out["value"] = {"detach": b.action_arg or ph_run_id or ""}
    else:
        # 兜底:历史 send=None 按钮 (e.g. 后台) 默认为 detach。
        out["value"] = {"detach": ph_run_id or ""}
    return out


def _btn_row(row: ButtonRow, *, ph_run_id: str | None) -> dict:
    return {
        "tag": "column_set",
        "columns": [
            {"tag": "column", "elements": [_btn(b, ph_run_id=ph_run_id)]}
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


def _render_answer(sec: AnswerSection, *, card_id: str) -> list[dict]:
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
    # tools — 全渲染。buf 里的不在这里出,等点按钮 reveal 才进 tools。
    for t in sec.tools:
        out.append(_panel(t.header, t.body_md or " ", folded=True))
    # tools overflow 槽位按钮:label 由 state 决定。
    # streaming:  "+N 待加载工具" (可点 reveal)
    # detached:   "正在后台调用工具" (静默占位,detach 瞬间冻结按钮)
    # terminal:   "显示剩余 N 工具" (可点 reveal)
    # cancelled / 无 buf: 不出
    if sec.tools_pending > 0:
        if sec.state == "detached":
            btn = Button(text="正在后台调用工具")
        elif sec.state == "terminal":
            btn = Button(
                text=f"显示剩余 {sec.tools_pending} 工具",
                action="load_tools", action_arg=card_id,
            )
        elif sec.state == "streaming":
            btn = Button(
                text=f"+{sec.tools_pending} 待加载工具",
                action="load_tools", action_arg=card_id,
            )
        else:
            btn = None
        if btn is not None:
            out.append(_btn_row(ButtonRow(buttons=(btn,)), ph_run_id=None))
    elif sec.state == "streaming" and not sec.tools and not sec.answer_text:
        out.append(_md("_正在组织回答_"))
    # 回答块:所有状态都用 collapsible_panel,保 detach 高度不变(spec §4.2)。
    if sec.state == "detached":
        ans_title, ans_body, ans_folded = "回答中…", "_回答仍在后台生成_", True
    elif sec.state == "terminal":
        ans_title, ans_body, ans_folded = "回答", sec.answer_text or " ", False
    elif sec.state == "cancelled":
        ans_title, ans_body, ans_folded = (
            "回答已取消", sec.answer_text or " ", True,
        )
    elif sec.answer_text:
        ans_title, ans_body, ans_folded = "回答", sec.answer_text, False
    else:
        ans_title, ans_body, ans_folded = "正在回答", "_…_", False
    out.append(_panel(ans_title, ans_body, folded=ans_folded))
    return out


def render(state: UnifiedCardState) -> dict:
    """Render a UnifiedCardState to a feishu interactive card message dict."""
    elements: list[dict] = []
    for row in state.top_buttons:
        elements.append(_btn_row(row, ph_run_id=state.ph_run_id))
    if state.compact is not None:
        elements.extend(_render_compact(state.compact))
    if state.answer is not None:
        elements.extend(_render_answer(state.answer, card_id=state.card_id))
    if state.menu is not None:
        elements.append(_md(state.menu.body_md))
    for row in state.bottom_buttons:
        elements.append(_btn_row(row, ph_run_id=state.ph_run_id))
    elements.append(_md(f"_{state.footer.render()}_"))
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "body": {"elements": elements},
    }
