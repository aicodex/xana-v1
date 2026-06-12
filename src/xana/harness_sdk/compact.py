"""Compact 共享纯函数 — auto-compact(DefaultHarness 内联触发)与 /compact 共用。

无 class、无 ABC;状态写回走已有 ActiveContextStore.set()。
boundary/summary/保留消息全部是普通 MemoryMessage,summary 字段不再使用。
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable, Sequence

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


SUMMARY_SECTIONS = (
    "主要请求与意图", "关键技术概念", "文件与代码部分", "错误与修复",
    "问题解决", "所有用户消息", "待处理任务", "当前工作", "可选的下一步",
)


def summary_progress_bar(text: str) -> str:
    """流式摘要文本 → 进度条。摘要格式固定为 9 个编号小节,
    见到第 n 节标题即认为前 n-1 节完成。"""
    done = 0
    current = "分析对话"
    for i, title in enumerate(SUMMARY_SECTIONS):
        if title in text:
            done = i  # 第 i+1 节开始 → 前 i 节完成
            current = title
    if "</summary>" in text:
        done = len(SUMMARY_SECTIONS)
        current = "完成"
    bar = "▰" * done + "▱" * (len(SUMMARY_SECTIONS) - done)
    return f"🗜 正在压缩上下文\n{bar} {done}/{len(SUMMARY_SECTIONS)} · {current}"


async def generate_summary(
    client, *, provider: str, model: str, messages: Sequence[MemoryMessage],
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> str | None:
    """同模型生成摘要: 无 tools、单回合;<summary> 缺失重试,3 次后放弃返回 None。"""
    def _plain(m: MemoryMessage) -> ModelMessage:
        # 摘要请求必须是纯文本转录:tool 消息降级为 user 文本,
        # assistant 的 tool_calls 元数据丢弃 — 否则 Anthropic 方言会拼出孤儿 tool_result(400)。
        if m.role == "tool":
            name = m.metadata.get("tool_name", "?")
            return ModelMessage(role="user", content=f"[工具 {name} 的结果]\n{m.content}")
        role = m.role if m.role in ("user", "assistant", "system") else "system"
        return ModelMessage(role=role, content=m.content)

    model_msgs = tuple(_plain(m) for m in messages) + (
        ModelMessage(role="user", content=get_compact_prompt()),)
    req = ModelRequest(provider=provider, model=model, messages=model_msgs, tools_schema=())
    for _ in range(_COMPACT_RETRIES):
        parts: list[str] = []
        try:
            async for event in client.stream(req):
                if event.kind == "text_delta":
                    parts.append(event.payload.get("text", ""))
                    if on_progress is not None:
                        await on_progress("".join(parts))
                elif event.kind in ("done", "error"):
                    break
        except Exception:
            continue  # 传输层异常 = 一次失败重试,与缺 <summary> 同等对待
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
