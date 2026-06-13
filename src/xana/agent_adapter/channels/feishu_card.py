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


def build_card(
    draft: TurnDraft,
    *,
    finalize: bool = False,
    buttons: list[list[dict]] | None = None,
    bg_runs: list | None = None,
) -> dict:
    """buttons=None → 用 draft.buttons;显式传入(如流式停止按钮)则覆盖。

    bg_runs:非 foreground 的 run 列表(RunRenderState 鸭子类型,需要 .run_id/.label)。
    非空时在卡片末尾追加一行 "切回 <label>" 按钮(最多 3 个,回调
    携带 {"action":"switch_foreground","run_id":...})。"""
    elements: list[dict] = []
    if draft.thinking or draft.thinking_title:
        # thinking_title 非空时即使正文为空也出面板(/compact 卡片布局从第一帧起不跳变)
        elements.append(_panel(draft.thinking_title or "💭 思考过程", draft.thinking or " "))
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
    elements.extend(_buttons_elements(draft.buttons if buttons is None else buttons))
    if bg_runs:
        # Feishu action rows get unwieldy beyond 3 buttons; truncated bg runs remain
        # reachable when their PH replaces the card.
        cols = []
        for r in list(bg_runs)[:3]:
            label = str(getattr(r, "label", "") or "")[:8]
            rid = str(getattr(r, "run_id", "") or "")
            if not rid:
                continue
            cols.append({
                "tag": "column", "width": "weighted", "weight": 1,
                "vertical_align": "center",
                "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"切回 {label}"},
                    "type": "default", "size": "medium",
                    "behaviors": [{
                        "type": "callback",
                        "value": {"action": "switch_foreground", "run_id": rid},
                    }],
                }],
            })
        if cols:
            elements.append({"tag": "column_set", "horizontal_spacing": "8px", "columns": cols})
    return {"schema": "2.0", "config": {"width_mode": "fill"}, "body": {"elements": elements}}


def build_completed_bg_card(draft: TurnDraft, label: str) -> dict:
    """PH 替换后的后台完成卡:✅ 后台已完成 chip + 完整内容 + 无 stop / 切回按钮。

    视觉上和"刚切走的冻结卡"明显不同(标题改为"已完成"),避免用户把它当
    成又开始 streaming 了;同时也和"前台正常 finalize 卡"区分开,告诉用户
    这是后台跑完没人看的那张。"""
    card = build_card(draft, finalize=True)
    card["header"] = {
        "title": {
            "tag": "plain_text",
            "content": f"✅ 后台已完成 · {label}",
        }
    }
    # build_card finalize=True 会保留 model buttons,但后台完成卡也保留(允许
    # 用户点击模型给的下一步建议)。strip column_set 的 stop / 切回 buttons:
    # finalize=True 已经没有 stop 了(rows = draft.buttons),不需要再过滤。
    return card


def build_frozen_ph_card(draft: TurnDraft, label: str) -> dict:
    """PH frozen 卡:header 黄灯 chip + 既有 thinking/tool/content 折叠;无按钮。

    detach / 切走前景时对该 run 的最后一次编辑;此后不再 patch 这张消息。"""
    card = build_card(draft, finalize=False)
    card["header"] = {
        "title": {
            "tag": "plain_text",
            "content": f"🟡 后台运行中 · {label}",
        }
    }
    card["body"]["elements"] = [
        el for el in card["body"]["elements"] if el.get("tag") != "action"
    ]
    return card


def _buttons_elements(rows: list[list[dict]]) -> list[dict]:
    """逐行移植 yuxu _draft_buttons_to_feishu(feishu.py:538-586):每行一个
    column_set,cell 为 weighted column 包一个 button,点击回调携带 send_text。"""
    out: list[dict] = []
    for row in rows or []:
        cols = []
        for btn in row or []:
            # copy 型:飞书 card 2.0 button.behaviors 只有 open_url/callback/form_action,
            # 无原生「复制到剪贴板」行为 → 回退为 callback send_text(点击 = 把文本发回输入)。
            text = str(btn.get("text") or btn.get("send") or btn.get("copy") or "").strip()
            send = str(btn.get("send") or btn.get("copy") or btn.get("text") or "").strip()
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
