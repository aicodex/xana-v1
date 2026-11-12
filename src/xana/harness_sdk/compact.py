"""摘要请求与输出校验；上下文生命周期由 ContextSDK 统一维护。"""
from __future__ import annotations
from contextlib import aclosing

from dataclasses import replace
import json
import logging
from typing import Awaitable, Callable

from xana.harness_sdk.compact_prompt import get_compact_prompt
from xana.llm_driver.contracts import ModelMessage, ModelRequest, ProviderError
from xana.llm_driver.interfaces import ModelClient
from xana.harness_sdk.contracts import AbortSignal
from xana.memory.contracts import MemoryMessage
from xana.context.sdk import estimate_tokens as _estimate_tokens
from xana.harness_sdk.stream_control import model_events

_logger = logging.getLogger(__name__)

class _CompactCancelled(Exception):
    """on_progress 回调里检测到用户取消 → 中止流。
    必须穿透 generate_summary 的重试循环(否则会被当成传输异常吞掉重试)。"""


_COMPACT_RETRIES = 3




SUMMARY_SECTIONS = (
    "主要请求与意图", "关键技术概念", "文件与代码部分", "错误与修复",
    "问题解决", "所有用户消息", "待处理任务", "当前工作", "可选的下一步",
)


def _summary_window(text: str) -> str | None:
    """真摘要窗口 = </analysis> 之后的第一个 <summary> 起。
    摘要正文可能复述 "<summary>"/小节标题(对话内容本身在讨论 compact 时必然如此),
    所以全文第一个(analysis 里的占位)和最后一个(正文里的复述)都不可靠 —
    必须锚定 analysis 结束后的第一个;窗口起点稳定,流式进度才单调。"""
    _, sep, after = text.rpartition("</analysis>")
    scan = after if sep else text
    start = scan.find("<summary>")
    if start == -1:
        return None
    return scan[start + len("<summary>"):]


def summary_progress(text: str) -> tuple[str, str]:
    """流式摘要文本 → (进度条正文, 当前小节状态文字)。
    摘要格式固定为 9 个编号小节,见到第 n 节标题即认为前 n-1 节完成。
    只扫真摘要窗口(_summary_window)。状态文字由调用方放进折叠块标题。"""
    total = len(SUMMARY_SECTIONS)
    seg = _summary_window(text)
    done = 0
    current = "分析对话"
    if seg is not None:
        for i, title in enumerate(SUMMARY_SECTIONS):
            if title in seg:
                done = i  # 第 i+1 节开始 → 前 i 节完成
                current = title
        if "</summary>" in seg:
            done = total
            current = "完成"
    bar = "▰" * done + "▱" * (total - done)
    return bar, current


def _overflow_request(request: ModelRequest, attempt: int) -> ModelRequest | None:
    """Kimi 725194d7: shrinkCompactionHistoryAfterOverflow, whole messages only.

    Ratios and recent-token selection are copied from fullCompactionService.ts.
    Never edit message text, media, call metadata or the live conversation.
    """
    ratios = (0.7, 0.5, 0.35)
    messages = request.messages
    first = 0
    while first < len(messages) and messages[first].role == "system":
        first += 1
    history = messages[first:]
    if len(history) <= 1:
        return None
    costs = [_estimate_tokens((MemoryMessage(m.role, m.content, m.metadata, m.media),)) for m in history]
    budget = int(sum(costs) * ratios[min(attempt - 1, len(ratios) - 1)])
    start, tokens = len(history), 0
    for index in range(len(history) - 1, -1, -1):
        if tokens + costs[index] > budget:
            break
        tokens += costs[index]
        start = index
    start = max(1, start)
    while start < len(history) and history[start].role == "tool":
        start += 1
    if start == len(history):
        return None
    return replace(request, messages=messages[:first] + history[start:])


def _complete_summary(text: str) -> str | None:
    """Accept the existing prompt's closed envelope; partial output is progress only."""
    import re
    match = re.fullmatch(r"\s*(?:<analysis>.*</analysis>\s*)?<summary>(.*)</summary>\s*", text, re.S)
    if match is None or re.search(r"</summary>\s*<summary>", match.group(1)):
        return None
    summary = match.group(1).strip()
    return summary or None


def _overflow_error(error: ProviderError | None) -> bool:
    if error is None or error.kind != "bad_request":
        return False
    text = (error.message + " " + json.dumps(error.raw, ensure_ascii=False)).lower()
    return any(term in text for term in (
        "context_length", "context window", "context length", "too many tokens", "prompt too long",
        "maximum context", "token limit", "context overflow"))


async def generate_summary(
    client: ModelClient, request: ModelRequest, *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    summary_limit_tokens: int | None = None,
    abort: AbortSignal | None = None,
) -> str | None:
    """Append the existing compact instruction to the ordinary request unchanged.

    No agent, session or alternate transcript is created. Only failed attempts may
    shrink the oldest history; generation never mutates the active conversation.
    Preserve output/thinking settings because output caps can change cache identity.
    """
    current = request
    signal = abort or AbortSignal()
    overflows = 0
    for attempt in range(_COMPACT_RETRIES):
        if signal.aborted:
            raise _CompactCancelled()
        req = replace(current, messages=current.messages + (ModelMessage("user", get_compact_prompt()),))
        parts: list[str] = []
        failure = "invalid_summary"
        completed = False
        tool_called = False
        try:
            async with aclosing(model_events(client.stream(req), signal)) as events:
                async for event in events:
                    if event.kind == "text_delta":
                        if event.payload.get("channel") != "thinking":
                            parts.append(event.payload.get("text", ""))
                            if on_progress is not None:
                                await on_progress("".join(parts))
                    elif event.kind == "tool_call":
                        tool_called = True  # Schemas are retained for caching; never execute tools here.
                    elif event.kind == "error":
                        if _overflow_error(event.error):
                            failure = "overflow"
                        elif event.error is not None and event.error.retryable:
                            failure = "transport"
                        else:
                            return None
                        break
                    elif event.kind == "done":
                        completed = event.payload.get("stop_reason") not in ("length", "max_tokens", "tool_calls")
                        break
        except _CompactCancelled:
            raise
        except Exception as exc:
            _logger.warning("compact attempt %s failed: %s", attempt + 1, type(exc).__name__)
            failure = "transport"
        if signal.aborted:
            raise _CompactCancelled()
        summary = _complete_summary("".join(parts)) if completed and not tool_called else None
        if summary and (summary_limit_tokens is None or _estimate_tokens((MemoryMessage("compact", summary),)) < summary_limit_tokens):
            return summary
        if failure == "overflow":
            overflows += 1
            smaller = _overflow_request(current, overflows)
            if smaller is None:
                return None
            current = smaller
        _logger.warning("compact attempt %s rejected (%s); original context retained", attempt + 1, failure)
    return None


def _extract_summary(text: str) -> str | None:
    """真摘要 = _summary_window 起、到最后一个 </summary>(缺失则到结尾)。
    闭合标签用 rfind:摘要正文里复述的 "</summary>" 不应提前截断。"""
    seg = _summary_window(text)
    if seg is None:
        return None
    end = seg.rfind("</summary>")
    if end != -1:
        seg = seg[:end]
    seg = seg.strip()
    return seg or None


# 公开别名:/compact 命令流式进度需要随时取「目前已生成的部分摘要」
partial_summary = _extract_summary
