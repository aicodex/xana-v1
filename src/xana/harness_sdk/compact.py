"""Compact 共享纯函数 — auto-compact(DefaultHarness 内联触发)与 /compact 共用。

无 class、无 ABC;状态写回走已有 ActiveContextStore.set()。
boundary/summary/保留消息全部是普通 MemoryMessage,summary 字段不再使用。
"""
from __future__ import annotations

import re
from typing import Sequence

from xana.harness_sdk.compact_prompt import (
    COMPACT_BOUNDARY_MARKER,
    POST_COMPACT_TEMPLATE,
    get_compact_prompt,
)
from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.memory.contracts import ActiveContext, MemoryMessage

_CHARS_PER_TOKEN = 4  # 与 DefaultUsageAccumulator 同源启发式
RECENT_KEEP_TOKENS = 20_000  # Codex 风格: 最近 user 消息逐字保留预算
BLOCKING_HEADROOM = 3_000    # blocking 线 = context_window - 3K
_COMPACT_RETRIES = 3
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_TRANSCRIPT_PLACEHOLDER = "(转录不可用)"


def estimate_tokens(messages: Sequence[MemoryMessage]) -> int:
    return max(1, sum(len(m.content or "") for m in messages) // _CHARS_PER_TOKEN)


def messages_since_boundary(messages: tuple[MemoryMessage, ...]) -> tuple[MemoryMessage, ...]:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].metadata.get("compact_boundary"):
            return messages[i + 1:]
    return messages


def recent_user_messages(
    messages: Sequence[MemoryMessage], budget_tokens: int = RECENT_KEEP_TOKENS
) -> tuple[MemoryMessage, ...]:
    kept: list[MemoryMessage] = []
    used = 0
    for m in reversed(messages):
        if m.role != "user":
            continue
        cost = max(1, len(m.content or "") // _CHARS_PER_TOKEN)
        if used + cost > budget_tokens:
            break
        kept.append(m)
        used += cost
    return tuple(reversed(kept))


async def generate_summary(
    client, *, provider: str, model: str, messages: Sequence[MemoryMessage]
) -> str | None:
    """同模型生成摘要: 无 tools、单回合;<summary> 缺失重试,3 次后放弃返回 None。"""
    model_msgs = tuple(
        ModelMessage(
            role=m.role if m.role in ("user", "assistant", "system", "tool") else "system",
            content=m.content,
        )
        for m in messages
    ) + (ModelMessage(role="user", content=get_compact_prompt()),)
    req = ModelRequest(provider=provider, model=model, messages=model_msgs, tools_schema=())
    for _ in range(_COMPACT_RETRIES):
        parts: list[str] = []
        async for event in client.stream(req):
            if event.kind == "text_delta":
                parts.append(event.payload.get("text", ""))
            elif event.kind in ("done", "error"):
                break
        match = _SUMMARY_RE.search("".join(parts))
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def build_post_compact_context(
    *, session_id: str, agent_name: str, summary: str, recent: tuple[MemoryMessage, ...]
) -> ActiveContext:
    boundary = MemoryMessage(
        role="system", content=COMPACT_BOUNDARY_MARKER, metadata={"compact_boundary": True}
    )
    summary_msg = MemoryMessage(
        role="user",
        content=POST_COMPACT_TEMPLATE.format(summary=summary, transcript=_TRANSCRIPT_PLACEHOLDER),
    )
    return ActiveContext(
        session_id=session_id, agent_name=agent_name,
        messages=(boundary, summary_msg) + recent,
    )
