"""DeepSeek V4 via the Anthropic /v1/messages dialect.

DeepSeek's ``/anthropic`` endpoint speaks Anthropic-Messages with one
known quirk: tool calls sometimes leak as raw DSML markup in the text
channel instead of as ``tool_use`` content blocks. We handle that by
buffering the accumulated text and running ``dsml_recovery`` once the
stream completes, yielding the recovered ``tool_call`` ModelEvents
before the final ``done`` event.

Auth: ``Authorization: Bearer <api_key>``. Endpoint:
``{base_url}/v1/messages`` (default base ``https://api.deepseek.com/anthropic``).

Wire shape produced (per xana ``ModelEventKind``):

  text_delta  ← Anthropic ``content_block_delta`` of type ``text_delta``
  tool_call   ← Anthropic ``content_block_stop`` for a ``tool_use`` block
                (input JSON accumulated across input_json_delta events)
              + post-stream recovered calls from leaked DSML markup
  rate_limited ← HTTP 429 (with optional Retry-After)
  error       ← HTTP 4xx/5xx other than 429, network errors, or shape errors
  usage       ← final accumulated usage (input + output tokens)
  done        ← end of stream (always last on a successful turn)

Scope NOT included in v1 (deferred): thinking-mode deltas, system-block
cache_control, native server-side web_search re-run (drop policy), tool
result message conversion (caller passes Anthropic-shape ``tools_schema``).
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, AsyncGenerator, AsyncIterator

import httpx

from xana.llm_driver.contracts import (
    ModelEvent,
    ModelRequest,
    ModelUsage,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorKind,
    RateLimitSignal,
)
from xana.llm_driver.interfaces import ModelClient
from xana.llm_driver.providers.dsml_recovery import DSML_FENCE_TAGS, parse_dsml_tool_calls
from xana.llm_driver.providers.stream_scrubber import StreamingContextScrubber


_logger = logging.getLogger(__name__)

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
_DEFAULT_MAX_TOKENS = 16384
_DSML_RECOVERED_CALL_ID_PREFIX = "dsml-recovered-"


@dataclass(frozen=True)
class AnthropicDeepSeekConfig:
    api_key: str
    base_url: str = _DEFAULT_BASE_URL
    max_tokens: int = _DEFAULT_MAX_TOKENS
    timeout_s: float = 60.0
    anthropic_version: str = _ANTHROPIC_VERSION


class AnthropicDeepSeekClient(ModelClient):
    """ModelClient for DeepSeek V4 over the Anthropic dialect.

    httpx.AsyncClient is created lazily so the same instance can be used
    across requests without leaking sockets; tests can inject a stub via
    the ``http_client`` constructor arg.
    """

    def __init__(
        self,
        config: AnthropicDeepSeekConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._http = http_client
        self._owns_http = http_client is None

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        body = _build_request_body(request, self._config)
        url = self._config.base_url.rstrip("/") + "/v1/messages"
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
            "anthropic-version": self._config.anthropic_version,
            "Accept": "text/event-stream",
        }

        http = self._http or httpx.AsyncClient()
        try:
            try:
                async with http.stream(
                    "POST", url, json=body, headers=headers, timeout=self._config.timeout_s
                ) as resp:
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("retry-after")
                        await resp.aread()
                        yield ModelEvent(
                            kind="rate_limited",
                            rate_limit=RateLimitSignal(
                                retry_after_s=float(retry_after) if retry_after else None,
                                reason=f"HTTP 429: {resp.text[:200]}",
                            ),
                        )
                        return
                    if resp.status_code >= 400:
                        await resp.aread()
                        yield ModelEvent(
                            kind="error",
                            error=ProviderError(
                                kind=_http_error_kind(resp.status_code),
                                message=f"HTTP {resp.status_code}: {resp.text[:300]}",
                                retryable=resp.status_code >= 500,
                                raw_status=resp.status_code,
                            ),
                        )
                        return
                    async for event in _parse_sse_stream(resp):
                        yield event
            except httpx.RequestError as exc:
                yield ModelEvent(
                    kind="error",
                    error=ProviderError(
                        kind="timeout" if isinstance(exc, httpx.TimeoutException) else "unknown",
                        message=f"{type(exc).__name__}: {exc}",
                        retryable=True,
                    ),
                )
        finally:
            if self._owns_http and self._http is None:
                await http.aclose()

    async def cancel(self, request_id: str) -> None:
        return None

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="anthropic_deepseek",
            supports_streaming=True,
            supports_tools=True,
            supports_cache_control=False,
            max_output_tokens=self._config.max_tokens,
        )

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()


SUPPORTED_EFFORTS = ("high", "max")  # DS API 实际支持;low/medium/xhigh 是兼容映射
_EFFORT_MAP = {"low": "high", "medium": "high", "high": "high",
               "xhigh": "max", "max": "max"}


def _build_request_body(request: ModelRequest, config: AnthropicDeepSeekConfig) -> dict[str, Any]:
    """Translate ModelMessages into Anthropic /v1/messages wire shape.

    Two tool-cycle details matter:

      * assistant messages with ``metadata['tool_calls']`` are rendered as
        content blocks: an optional text block followed by one tool_use block
        per call. Without this the next request would only ship the assistant
        text and DS would forget what it had asked for.
      * adjacent ``role='tool'`` messages are collapsed into one user message
        whose content is the list of tool_result blocks (Anthropic requires
        all tool_results for a single assistant tool_use round to live in one
        user turn, not separate messages).
    """
    system_parts: list[str] = []
    history: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def _flush_pending() -> None:
        if pending_tool_results:
            history.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for msg in request.messages:
        if msg.role == "system":
            _flush_pending()
            if msg.content:
                system_parts.append(msg.content)
            continue
        if msg.role == "user":
            _flush_pending()
            # 连续 user(用户消息 + harness 追加的 ContextNote)合并为一轮的多个文本块,
            # 不依赖端点自行合并相邻同角色轮次。
            if history and history[-1]["role"] == "user":
                prev = history[-1]["content"]
                blocks = prev if isinstance(prev, list) else [{"type": "text", "text": prev}]
                history[-1]["content"] = blocks + [{"type": "text", "text": msg.content}]
            else:
                history.append({"role": "user", "content": msg.content})
            continue
        if msg.role == "assistant":
            _flush_pending()
            tool_calls = (msg.metadata or {}).get("tool_calls") or []
            if tool_calls:
                blocks: list[dict[str, Any]] = []
                # DS 要求工具调用轮次回传 reasoning_content,否则后续请求 400;
                # 线格式为 Anthropic thinking block,且必须在最前。
                reasoning = (msg.metadata or {}).get("reasoning_content")
                if reasoning:
                    blocks.append({"type": "thinking", "thinking": reasoning})
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                for tc in tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("call_id", ""),
                            "name": tc.get("tool_name", ""),
                            "input": tc.get("input", {}) or {},
                        }
                    )
                history.append({"role": "assistant", "content": blocks})
            else:
                history.append({"role": "assistant", "content": msg.content})
            continue
        if msg.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": (msg.metadata or {}).get("tool_use_id", ""),
                "content": msg.content,
            }
            if (msg.metadata or {}).get("ok") is False:
                block["is_error"] = True        # 同 CC:失败的工具结果带 is_error
            pending_tool_results.append(block)
            continue
    _flush_pending()

    body: dict[str, Any] = {
        "model": request.model,
        "max_tokens": config.max_tokens,
        "messages": history,
        "stream": True,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if request.tools_schema:
        body["tools"] = [dict(t) for t in request.tools_schema]
    # native web_search:harness 在互斥剔除函数版 web_search 后经 metadata
    # 通知此处 append server tool(verbatim 形状,不得过工具转换器)。
    if (request.metadata or {}).get("web_search_native"):
        body.setdefault("tools", []).append(
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 5})
    # request.metadata["thinking"] = "disabled"/"enabled" → Anthropic 方言 thinking 开关
    # (compact 摘要调用必须关思考;缺省不发,保持原行为)
    thinking = (request.metadata or {}).get("thinking")
    if thinking in ("disabled", "enabled"):
        body["thinking"] = {"type": thinking}
    # DS API: {"output_config": {"effort": "high"|"max"}};兼容值就近映射
    effort = _EFFORT_MAP.get(str(request.effort).lower()) if request.effort else None
    if effort:
        body["output_config"] = {"effort": effort}
    elif request.effort:
        # 旧配置可能残留映射外的值——静默丢弃会让 /effort 看似生效实则无效
        _logger.warning("未知 effort %r,跳过 output_config", request.effort)
    return body


def _http_error_kind(status: int) -> ProviderErrorKind:
    if status in (401, 403):
        return "auth"
    if status in (400, 422):
        return "bad_request"
    if status >= 500:
        return "server_error"
    return "unknown"


def _delta_to_events(delta: dict[str, Any]) -> list[ModelEvent]:
    """Map one stream delta dict to text_delta ModelEvents.

    Accepts both the OpenAI-compatible spelling (``reasoning_content`` /
    ``content``) and the Anthropic-dialect spelling (``thinking`` / ``text``).
    Reasoning text rides the locked ModelEvent payload as
    ``payload["channel"] == "thinking"`` (no new ModelEventKind values);
    answer text carries no channel key. When both are present, the thinking
    event comes first.
    """
    events: list[ModelEvent] = []
    reasoning = delta.get("reasoning_content") or delta.get("thinking")
    if reasoning:
        events.append(
            ModelEvent(kind="text_delta", payload={"text": reasoning, "channel": "thinking"})
        )
    content = delta.get("content") or delta.get("text")
    if content:
        events.append(ModelEvent(kind="text_delta", payload={"text": content}))
    return events


_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _merge_usage(acc: dict[str, int], usage: dict | None) -> None:
    """message_start/message_delta 的 usage 逐字段合并 — 后到的非空值覆盖。"""
    if not usage:
        return
    for key in _USAGE_KEYS:
        val = usage.get(key)
        if val is not None:
            acc[key] = int(val)


async def _parse_sse_stream(resp: httpx.Response) -> AsyncGenerator[ModelEvent, None]:
    """Parse Anthropic-style SSE → ModelEvent sequence + DSML recovery on end.

    Buffers per-tool input_json deltas and emits a single ``tool_call`` event
    on ``content_block_stop``. Accumulates the full text channel; once the
    SSE stream ends, scans the buffer for DSML tool-call leaks and emits
    any recovered calls as additional ``tool_call`` events. Always ends
    with a single ``done`` event after ``usage``.
    """
    text_buffer: list[str] = []
    blocks: dict[int, dict[str, Any]] = {}
    usage_acc: dict[str, int] = {}
    # Per-stream scrubber: yielded text_delta events are scrubbed of raw
    # DSML fences; the RAW text still enters text_buffer for recovery.
    scrubber = StreamingContextScrubber(DSML_FENCE_TAGS)

    event_type: str | None = None
    data_lines: list[str] = []

    stop_reason: str | None = None
    async for raw_line in resp.aiter_lines():
        if raw_line == "":
            if event_type is not None and data_lines:
                payload = _decode_sse_data(data_lines)
                if payload is not None:
                    async for ev in _dispatch_sse_event(
                        event_type, payload, blocks, text_buffer, scrubber
                    ):
                        yield ev
                    if event_type == "message_start":
                        _merge_usage(usage_acc, (payload.get("message") or {}).get("usage"))
                    elif event_type == "message_delta":
                        _merge_usage(usage_acc, payload.get("usage"))
                        stop_reason = (payload.get("delta") or {}).get("stop_reason") or stop_reason
            event_type, data_lines = None, []
            continue
        if raw_line.startswith(":"):
            continue
        if raw_line.startswith("event:"):
            event_type = raw_line[len("event:"):].strip()
        elif raw_line.startswith("data:"):
            data_lines.append(raw_line[len("data:"):].lstrip())

    if event_type is not None and data_lines:
        payload = _decode_sse_data(data_lines)
        if payload is not None:
            async for ev in _dispatch_sse_event(
                event_type, payload, blocks, text_buffer, scrubber
            ):
                yield ev

    tail = scrubber.flush()
    if tail:
        yield ModelEvent(kind="text_delta", payload={"text": tail})

    recovered_text = "".join(text_buffer)
    _, leaked = parse_dsml_tool_calls(recovered_text)
    if leaked:
        _logger.warning(
            "DSML_LEAK_WORKAROUND deepseek endpoint leaked %d tool call(s) as raw "
            "DSML; recovered client-side: %s", len(leaked), [c["name"] for c in leaked])
    for call in leaked:
        yield ModelEvent(
            kind="tool_call",
            payload={
                "tool_name": call["name"],
                "input": call["input"],
                "call_id": f"{_DSML_RECOVERED_CALL_ID_PREFIX}{uuid.uuid4().hex[:12]}",
            },
        )

    yield ModelEvent(
        kind="usage",
        usage=ModelUsage(
            input_tokens=usage_acc.get("input_tokens"),
            output_tokens=usage_acc.get("output_tokens"),
            cache_read_tokens=usage_acc.get("cache_read_input_tokens"),
            cache_write_tokens=usage_acc.get("cache_creation_input_tokens"),
        ),
    )
    # stop_reason 归一:max_tokens → length(与 OpenAI 方言一致)
    yield ModelEvent(kind="done", payload={
        "stop_reason": "length" if stop_reason == "max_tokens" else (stop_reason or "")})


def _decode_sse_data(data_lines: list[str]) -> dict[str, Any] | None:
    raw = "\n".join(data_lines)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


async def _dispatch_sse_event(
    event_type: str,
    payload: dict[str, Any],
    blocks: dict[int, dict[str, Any]],
    text_buffer: list[str],
    scrubber: StreamingContextScrubber,
) -> AsyncGenerator[ModelEvent, None]:
    if event_type == "content_block_start":
        idx = int(payload.get("index", 0))
        block = payload.get("content_block") or {}
        blocks[idx] = {
            "type": block.get("type"),
            "name": block.get("name"),
            "id": block.get("id"),
            "partial_json": "",
        }
        return

    if event_type == "content_block_delta":
        idx = int(payload.get("index", 0))
        delta = payload.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            text = delta.get("text") or ""
            # RAW text feeds the post-stream DSML recovery scan; the
            # yielded event carries only the scrubbed visible portion.
            text_buffer.append(text)
            visible = scrubber.feed(text)
            if visible:
                for ev in _delta_to_events({"content": visible}):
                    yield ev
        elif dtype in ("thinking_delta", "reasoning_content_delta"):
            # Reasoning never enters text_buffer: it must not leak into the
            # final answer text nor the post-stream DSML recovery scan.
            reasoning = delta.get("thinking") or delta.get("reasoning_content") or ""
            for ev in _delta_to_events({"reasoning_content": reasoning}):
                yield ev
        elif dtype == "input_json_delta":
            partial = delta.get("partial_json") or ""
            block = blocks.setdefault(idx, {"partial_json": ""})
            block["partial_json"] = block.get("partial_json", "") + partial
        return

    if event_type == "content_block_stop":
        idx = int(payload.get("index", 0))
        block = blocks.get(idx)
        if block and block.get("type") == "tool_use":
            try:
                tool_input = json.loads(block.get("partial_json") or "{}")
            except json.JSONDecodeError:
                tool_input = {}
            yield ModelEvent(
                kind="tool_call",
                payload={
                    "tool_name": block.get("name") or "",
                    "input": tool_input,
                    "call_id": block.get("id") or "",
                },
            )
        return

    # message_start / message_delta / message_stop / ping — usage is read by
    # the outer parser. Nothing to yield here, but keep the empty `yield`
    # below so the function remains an async generator.
    if False:
        yield  # pragma: no cover
