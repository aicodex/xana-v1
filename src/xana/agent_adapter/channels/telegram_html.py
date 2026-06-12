"""src/xana/agent_adapter/channels/telegram_html.py

Telegram HTML parse-mode 渲染。thinking/工具块用 <blockquote expandable>
(默认折叠,一 tap 展开——与飞书 collapsible_panel 同 UX)。
单消息 4096 字符上限;clip_html 预留标签余量截到 3900。
移植自 yuxu_backup adapters/telegram.py:599-660。
"""
from __future__ import annotations

import html as _html
import json
import re

from xana.agent_adapter.channels._draft import ToolBlock, TurnDraft, clip_body

_TG_LIMIT = 3900


def md_to_html(text: str) -> str:
    """极简 markdown→Telegram HTML: 先转义,再 ```pre``` / `code` / **bold**。"""
    s = _html.escape(text or "")
    s = re.sub(r"```(?:\w*\n)?(.*?)```", r"<pre>\1</pre>", s, flags=re.S)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
    return s


def clip_html(s: str, limit: int = _TG_LIMIT) -> str:
    return s if len(s) <= limit else s[:limit] + "…"


def render_thinking(thinking: str) -> str:
    return ("💭 <b>Thinking</b>\n"
            f"<blockquote expandable>{md_to_html(clip_body(thinking))}</blockquote>")


def render_tool_block(blk: ToolBlock) -> str:
    head = _html.escape(blk.header())
    body_parts: list[str] = []
    if blk.args:
        body_parts.append("<pre>" + _html.escape(
            clip_body(json.dumps(blk.args, ensure_ascii=False, indent=2))) + "</pre>")
    if blk.running:
        body_parts.append("⏳ 执行中…")
    elif blk.result:
        body_parts.append(md_to_html(clip_body(blk.result)))
    return clip_html(f"<b>{head}</b>\n<blockquote expandable>" + "\n".join(body_parts) + "</blockquote>")


def render_streaming(draft: TurnDraft) -> str:
    """流式中的单消息: [thinking 折叠块] + 正文。工具块独立 sealed 消息,不在此。"""
    parts: list[str] = []
    if draft.thinking:
        parts.append(render_thinking(draft.thinking))
    if draft.content:
        parts.append(md_to_html(draft.content))
    return clip_html("\n".join(parts) or "…")


def render_answer(draft: TurnDraft) -> str:
    parts: list[str] = []
    if draft.content:
        parts.append(md_to_html(draft.content))
    if draft.footer_meta:
        meta = " | ".join(f"{k}: {v}" for k, v in draft.footer_meta)
        parts.append("――――――――――――\n<i>" + _html.escape(meta) + "</i>")
    return clip_html("\n".join(parts).strip() or " ")


def buttons_to_reply_markup(rows: list[list[dict]]) -> dict | None:
    """[[{text,send}],...] → InlineKeyboardMarkup。callback_data='btn:{r}:{c}'
    索引(64 字节限制);真实文本由 adapter 按轮缓存反查。"""
    keyboard = [[{"text": b["text"][:64], "callback_data": f"btn:{r}:{c}"}
                 for c, b in enumerate(row)] for r, row in enumerate(rows or []) if row]
    return {"inline_keyboard": keyboard} if keyboard else None
