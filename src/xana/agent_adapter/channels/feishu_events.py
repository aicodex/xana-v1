"""Feishu event parsing.

Targets the "v2" event format (header + event envelopes). Recognizes
`im.message.receive_v1` messages of type `text` and `post` (rich text: Feishu sends
multi-line / formatted / linked messages as post), flattened to plain text.
Anything else returns None from parse_message_event() and the adapter ignores it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)
TEXT_TYPES = ("text", "post")


@dataclass(frozen=True)
class ParsedMessageEvent:
    chat_id: str
    chat_type: str  # "p2p" | "group"
    message_id: str
    message_type: str
    text: str
    sender_open_id: str | None


def parse_message_event(event: dict[str, Any]) -> ParsedMessageEvent | None:
    header = event.get("header") or {}
    if header.get("event_type") != "im.message.receive_v1":
        return None
    body = event.get("event") or {}
    msg = body.get("message") or {}
    message_type = msg.get("message_type")
    if message_type not in TEXT_TYPES:
        _logger.info("飞书消息类型 %s 暂不支持,已忽略(message_id=%s)", message_type, msg.get("message_id"))
        return None
    content_raw = msg.get("content") or "{}"
    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError:
        return None
    text = (str(content.get("text", "")) if message_type == "text" else post_text(content)).strip()
    if not text:
        return None
    sender = body.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    return ParsedMessageEvent(
        chat_id=str(msg.get("chat_id") or ""),
        chat_type=str(msg.get("chat_type") or "p2p"),
        message_id=str(msg.get("message_id") or ""),
        message_type=str(msg.get("message_type") or "text"),
        text=text,
        sender_open_id=sender_id.get("open_id"),
    )


def post_text(content: dict[str, Any]) -> str:
    """富文本(post)转纯文本:标题一行,每段一行;链接保留地址,@ 保留名字,图片 / 视频写占位。
    接收事件里的 content 是 {"title", "content": [[元素...], ...]},个别客户端外面包一层语言键。"""
    if "content" not in content and content:
        content = next((v for v in content.values() if isinstance(v, dict)), {})
    lines = [str(content.get("title") or "").strip()]
    for paragraph in content.get("content") or []:
        parts: list[str] = []
        for el in paragraph or []:
            tag = el.get("tag")
            if tag in ("text", "md"):
                parts.append(str(el.get("text", "")))
            elif tag == "a":
                label, href = str(el.get("text", "")), str(el.get("href", ""))
                parts.append(f"{label}({href})" if label and href and label != href else href or label)
            elif tag == "at":
                parts.append(f"@{el.get('user_name') or el.get('user_id') or ''}")
            elif tag == "code_block":
                parts.append(f"```{el.get('language', '')}\n{el.get('text', '')}\n```")
            elif tag == "img":
                parts.append("[图片]")
            elif tag == "media":
                parts.append("[视频]")
            elif tag == "emotion":
                parts.append(f"[{el.get('emoji_type', '表情')}]")
        lines.append("".join(parts))
    return "\n".join(line for line in lines if line.strip())
