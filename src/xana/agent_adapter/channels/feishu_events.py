"""Feishu event parsing + HMAC signature verification.

Targets the "v2" event format (header + event envelopes). For MVP we only
recognize `im.message.receive_v1` text messages. Anything else returns None
from parse_message_event() and the adapter ignores it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any


class FeishuVerifyError(Exception):
    pass


def verify_signature(*, timestamp: str, nonce: str, body: bytes,
                     encrypt_key: str, signature: str) -> None:
    """Raise FeishuVerifyError if `signature` doesn't match the canonical digest."""
    if not (timestamp and nonce and encrypt_key and signature):
        raise FeishuVerifyError("missing signature components")
    to_sign = (timestamp + nonce + encrypt_key).encode("utf-8") + body
    digest = hashlib.sha256(to_sign).hexdigest()
    if not hmac.compare_digest(digest, signature):
        raise FeishuVerifyError("signature mismatch")


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
    if msg.get("message_type") != "text":
        return None
    content_raw = msg.get("content") or "{}"
    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError:
        return None
    text = str(content.get("text", "")).strip()
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
