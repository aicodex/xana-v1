"""飞书 renderer: UnifiedCardState -> 飞书 interactive card schema dict。

输出可直接喂 lark send_message / patch_message。
Section 顺序固定:top buttons -> compact -> answer -> menu -> bottom buttons -> footer。

飞书卡内容上限约 30KB,本 renderer 给每 tool body 限 800 字,避免超限。
不实现 expand 模式(TG-only)—— state.expand_mode 字段在飞书侧直接忽略。
"""
from __future__ import annotations

import re

from xana.agent_adapter.channels.process_view import build_view, policy_for
from xana.agent_adapter.channels.unified_card import (
    progress_line,
    AnswerSection,
    Button,
    ButtonRow,
    CompactSection,
    UnifiedCardState,
)


# 飞书 markdown 会把 ![alt](url) / 单独 URL 当 image_key 解析,带未知 URL 直接
# patch 400(200570 invalid image keys)。在 body 进卡前替换成纯文本链接。
_RE_MD_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(([^)\s\n]+)\)")


def _sanitize_md(body: str) -> str:
    """把 ![alt](url) 替换成 `[图片: url]` 纯文本,避开飞书 image_key 误判。"""
    if not body:
        return body
    return _RE_MD_IMAGE.sub(
        lambda m: f"[图片: {m.group(1) or m.group(2)}]", body)


def _progress_bar(cells: int) -> str:
    cells = max(0, min(10, cells))
    return "▰" * cells + "▱" * (10 - cells)


def _md(text: str) -> dict:
    return {"tag": "markdown", "content": text}


def _panel(title: str, body_md: str, *, folded: bool = True) -> dict:
    # 视觉提示:灰底卡片 + header 右侧 chevron(down-bold_outlined),
    # 展开旋转 -180° 变上箭头 → 用户一眼看出能点。
    # body 走 _sanitize_md:避开飞书 markdown 把 ![](url) 当 image_key 触发 400。
    return {
        "tag": "collapsible_panel",
        "expanded": not folded,
        "background_color": "grey-50",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "background_color": "grey-50",
            "vertical_align": "center",
            "padding": "4px 8px 4px 8px",
            "icon": {
                "tag": "standard_icon",
                "token": "down-bold_outlined",
                "color": "grey",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "elements": [_md(_sanitize_md(body_md or " "))],
    }


def _btn(b: Button, *, ph_run_id: str | None) -> dict:
    """飞书 card v2:button 点击 callback 必须包在 ``behaviors`` 里;
    裸 ``value`` 在 v2 schema 下不触发(用户报的'按钮报错'根因)。
    """
    if b.send:
        value: dict = {"send_text": b.send}
    elif b.action == "load_tools":
        value = {"load_tools": b.action_arg or ""}
    elif b.action == "detach":
        value = {"detach": b.action_arg or ph_run_id or ""}
    else:
        value = {"detach": ph_run_id or ""}
    return {
        "tag": "button",
        "type": "default",
        "size": "medium",
        "text": {"tag": "plain_text", "content": b.text},
        "behaviors": [{"type": "callback", "value": value}],
    }


def _btn_row(row: ButtonRow, *, ph_run_id: str | None) -> dict:
    return {
        "tag": "column_set",
        "columns": [
            {"tag": "column", "elements": [_btn(b, ph_run_id=ph_run_id)]}
            for b in row.buttons
        ],
    }


def _render_compact(sec: CompactSection) -> list[dict]:
    """一行进度条+状态;下方折叠"已压缩 N 条消息"。
    不再分两行(进度条+独立标题),归档措辞留给 /clear。"""
    out: list[dict] = []
    if sec.state == "completed":
        status = "完成压缩"
    elif sec.state == "cancelled":
        status = "压缩已取消"
    else:
        status = "压缩中"
    if sec.state == "cancelled":
        out.append(_md(f"**{status}**"))
    else:
        # 进度条 + 状态同一行
        out.append(_md(f"{_progress_bar(sec.progress_cells)} **{status}**"))
    if sec.state == "completed":
        panel_title = f"已压缩 {sec.compacted_n} 条消息"
    elif sec.state == "cancelled":
        panel_title = "压缩已取消"
    else:
        panel_title = "压缩中"
    out.append(_panel(panel_title, sec.summary_text or " ", folded=True))
    return out


def _render_process(sec: AnswerSection, mode: str) -> list[dict]:
    """grouped / summary:按过程组或整轮渲染;失败的调用单独一行红字,不折叠。"""
    alive = sec.state in ("streaming", "detached")
    view = build_view(sec, mode, alive=alive)
    out: list[dict] = []
    for b in view.blocks:
        out.append(_panel(b.title, b.body, folded=True))
    if view.line and alive:
        out.append(_md(f"<font color='grey'>› {view.line}</font>"))
    for line in view.failed:
        out.append(_md(f"<font color='red'>{line}</font>"))
    return out


def _render_answer(
    sec: AnswerSection, *, card_id: str, from_bg: bool = False, view_mode: str = "detailed",
) -> list[dict]:
    out: list[dict] = []
    # thinking — 默认折叠,空内容不放占位(只动 body,不动标题状态)
    think_title = "❋ 思考过程" if sec.thinking_text else "❋ 思考中"
    out.append(_panel(think_title, sec.thinking_text or " ", folded=True))
    policy = policy_for(view_mode)
    if policy.unit != "call":
        out.extend(_render_process(sec, view_mode))
    # tools — 全渲染。单 tool body 截 800 字符:飞书卡总大小有上限(230025),
    # 多 tool 全 body 会超限 → patch_card 400 → UI 卡住。
    for t in (sec.tools if policy.unit == "call" else ()):
        body = t.body_md or " "
        if len(body) > 800:
            body = body[:800].rstrip() + "\n…(已截断)"
        out.append(_panel(t.header, body, folded=True))
    # tools overflow 槽位:
    #   detached: 不可点的静态文字(detach 期间不接受交互)
    #   streaming / terminal: 可点按钮 → reveal 当前 buf
    #   cancelled / 空 buf: 不出
    if sec.tools_pending > 0 and policy.unit == "call":
        if sec.state == "detached":
            out.append(_md("_后台 ✎ 中_"))
        elif sec.state in ("streaming", "terminal"):
            label = f"展开 ✎×{sec.tools_pending}"
            btn = Button(text=label, action="load_tools", action_arg=card_id)
            out.append(_btn_row(ButtonRow(buttons=(btn,)), ph_run_id=None))
    # 进度行(§5.5a):进行中显示最近一条进度说明,灰色一行
    line = progress_line(sec) if policy.progress_line else ""
    if line:
        out.append(_md(f"<font color='grey'>{line}</font>"))
    # 回答块:标题状态机保留,空 body 用空格(不放占位)。
    ans_title = "✦ 回答" if sec.answer_text else "✦ 回答中"
    if sec.state == "cancelled":
        ans_title, ans_folded = "✦ 回答已取消", True
    elif sec.state == "detached":
        ans_folded = True
    elif sec.state == "terminal":
        ans_folded = from_bg
    else:
        ans_folded = False
    out.append(_panel(ans_title, sec.answer_text or " ", folded=ans_folded))
    return out


def render(state: UnifiedCardState) -> dict:
    """Render a UnifiedCardState to a feishu interactive card message dict。

    飞书 renderer 始终是 default 模式 —— expand_mode 字段被忽略。
    """
    elements: list[dict] = []
    for row in state.top_buttons:
        elements.append(_btn_row(row, ph_run_id=state.ph_run_id))
    if state.compact is not None:
        elements.extend(_render_compact(state.compact))
    if state.answer is not None:
        elements.extend(_render_answer(
            state.answer, card_id=state.card_id,
            from_bg=state.terminal_from_bg, view_mode=state.view_mode,
        ))
    if state.menu is not None:
        elements.append(_md(state.menu.body_md))
    for row in state.bottom_buttons:
        elements.append(_btn_row(row, ph_run_id=state.ph_run_id))
    footer_text = state.footer.render()
    if footer_text:
        elements.append(_md(f"_{footer_text}_"))
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "body": {"elements": elements},
    }
