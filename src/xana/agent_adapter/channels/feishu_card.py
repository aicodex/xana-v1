"""src/xana/agent_adapter/channels/feishu_card.py

飞书互动卡片 Schema 2.0 构建。布局(流式与 finalize 结构一致,卡片不跳变):
    [collapsible_panel: 💭 思考过程 — 有 thinking 才有, 始终折叠]
    [collapsible_panel × N: 工具调用 — xanac 风格标题, 始终折叠]
    [markdown: 回答正文]
    [hr + 灰字脚标]
    [column_set × N: 选项按钮]
移植自 yuxu_backup adapters/feishu.py:449-672。collapsible_panel 的
header.icon 必填——缺了飞书静默丢弃整个面板(yuxu 实测注释)。
"""
from __future__ import annotations

import json

from xana.agent_adapter.channels._draft import ToolBlock, TurnDraft, clip_body

_PANEL_HEADER_COMMON = {
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
}


def _panel(title: str, body_md: str) -> dict:
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "background_color": "grey-50",
        "header": {"title": {"tag": "plain_text", "content": title}, **_PANEL_HEADER_COMMON},
        "elements": [{"tag": "markdown", "content": body_md}],
    }


def _tool_panel(blk: ToolBlock) -> dict:
    parts: list[str] = []
    if blk.args:
        parts.append("```json\n" + clip_body(json.dumps(blk.args, ensure_ascii=False, indent=2)) + "\n```")
    if blk.running:
        parts.append("_⏳ 执行中…_")
    else:
        parts.append(clip_body(blk.result) if blk.result else f"_(no output recorded for `{blk.name}`)_")
    return _panel(blk.header(), "\n".join(parts))


def build_card(draft: TurnDraft, *, finalize: bool) -> dict:
    elements: list[dict] = []
    if draft.thinking:
        elements.append(_panel("💭 思考过程", draft.thinking))
    for blk in draft.tool_blocks:
        elements.append(_tool_panel(blk))
    if draft.content:
        elements.append({"tag": "markdown", "content": draft.content})
    elif not elements:
        elements.append({"tag": "markdown", "content": " "})  # 飞书拒绝空 markdown
    if draft.footer_meta:
        elements.append({"tag": "hr"})
        meta = " | ".join(f"{k}: {v}" for k, v in draft.footer_meta)
        elements.append({"tag": "markdown", "content": f"<font color='grey'>{meta}</font>"})
    elements.extend(_buttons_elements(draft.buttons))
    return {"schema": "2.0", "config": {"width_mode": "fill"}, "body": {"elements": elements}}


def _buttons_elements(rows: list[list[dict]]) -> list[dict]:
    """逐行移植 yuxu _draft_buttons_to_feishu(feishu.py:538-586):每行一个
    column_set,cell 为 weighted column 包一个 button,点击回调携带 send_text。"""
    out: list[dict] = []
    for row in rows or []:
        cols = []
        for btn in row or []:
            text = str(btn.get("text") or btn.get("send") or "").strip()
            send = str(btn.get("send") or btn.get("text") or "").strip()
            if not text or not send:
                continue
            cols.append({
                "tag": "column", "width": "weighted", "weight": 1,
                "vertical_align": "center",
                "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": text},
                    "type": "default", "size": "medium",
                    "behaviors": [{"type": "callback", "value": {"send_text": send}}],
                }],
            })
        if cols:
            out.append({"tag": "column_set", "horizontal_spacing": "8px", "columns": cols})
    return out
