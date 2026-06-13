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


def render_thinking(thinking: str, title: str | None = None) -> str:
    head = _html.escape(title) if title else "💭 <b>Thinking</b>"
    body = md_to_html(clip_body(thinking)) or " "  # 空内容也保留块(布局不跳变)
    return f"{head}\n<blockquote expandable>{body}</blockquote>"


def render_tool_block(blk: ToolBlock) -> str:
    """单折叠工具块(与 yuxu _render_one_tool_block_html 同构)。

    头行: `<b>● Tool(preview) — FAILED/⏳/Nms</b>`(header 已含运行/失败/耗时后缀)。
    折叠体: `escape(args_json)` + `\n────────\n` + `escape(result)`,单个
    `<blockquote expandable>`。**不嵌 `<pre>`、不对 result 跑 md_to_html**——
    否则 result 里的 markdown 会再生成 `<pre>`/`<a>` 标签,Telegram 会把折叠块
    拆成多块。running 时折叠体只有 args(运行状态由头行的 ⏳ 表达)。"""
    head = _html.escape(blk.header())
    body_segments: list[str] = []
    if blk.args:
        body_segments.append(_html.escape(
            clip_body(json.dumps(blk.args, ensure_ascii=False, indent=2))))
    if not blk.running and blk.result:
        body_segments.append(_html.escape(clip_body(blk.result)))
    body = "\n────────\n".join(body_segments)
    return clip_html(f"<b>{head}</b>\n<blockquote expandable>{body}</blockquote>")


def render_streaming(draft: TurnDraft) -> str:
    """流式中的单消息: [thinking 折叠块] + 正文 + 脚标(常驻,随 usage_progress 变)。
    工具块独立 sealed 消息,不在此。"""
    parts: list[str] = []
    if draft.thinking or draft.thinking_title:
        parts.append(render_thinking(draft.thinking, draft.thinking_title))
    if draft.content:
        parts.append(md_to_html(draft.content))
    if draft.footer_meta:
        parts.append(_footer_html(draft.footer_meta))
    return clip_html("\n".join(parts) or "…")


def _footer_html(footer_meta: list[tuple[str, str]]) -> str:
    meta = " | ".join(f"{k}: {v}" for k, v in footer_meta)
    return "――――――――――――\n<i>" + _html.escape(meta) + "</i>"


def render_answer(draft: TurnDraft) -> str:
    parts: list[str] = []
    if draft.content:
        parts.append(md_to_html(draft.content))
    if draft.footer_meta:
        parts.append(_footer_html(draft.footer_meta))
    return clip_html("\n".join(parts).strip() or " ")


def _markup_button(b: dict, r: int, c: int) -> dict:
    """send 型 → callback_data='btn:{r}:{c}'(64 字节限制;真实文本 adapter 按轮反查);
    copy 型 → Bot API copyTextButton(无 callback_data,TG 客户端本地复制到输入框)。"""
    text = str(b.get("text", ""))[:64]
    if "copy" in b:
        return {"text": text, "copy_text": {"text": str(b.get("copy", ""))[:256]}}
    return {"text": text, "callback_data": f"btn:{r}:{c}"}


def buttons_to_reply_markup(rows: list[list[dict]]) -> dict | None:
    """[[{text,send} | {text,copy}],...] → InlineKeyboardMarkup。"""
    keyboard = [[_markup_button(b, r, c) for c, b in enumerate(row)]
                for r, row in enumerate(rows or []) if row]
    return {"inline_keyboard": keyboard} if keyboard else None
