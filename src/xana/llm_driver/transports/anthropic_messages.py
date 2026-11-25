"""Anthropic Messages wire dialect transport (DeepSeek Anthropic + Anthropic-compatible).

Preserves exact DeepSeek/Anthropic invariants:
- `system` messages joined with "\\n\\n"
- consecutive `user` messages merged into content blocks
- adjacent `tool` messages collapsed into one `user` turn with `tool_result` blocks (`is_error`)
- assistant tool-call turns replay `thinking` block FIRST before `text` and `tool_use`
- optional DSML fence scrubbing & post-stream recovery (`profile.enable_dsml_recovery`)
- native `web_search_20250305` passthrough (`profile.supports_native_web_search`)
- `stop_reason == "max_tokens"` normalized to `"length"`
- mid-stream EOF without `message_stop`/`stop_reason` emits retryable `stream_eof` error
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncGenerator

import httpx

from xana.llm_driver.contracts import (
    ModelEvent,
    ModelRequest,
    ModelUsage,
    ProviderCapabilities,
    ProviderError,
    ProviderProfile,
)
from xana.llm_driver.errors import (
    classify_exception,
    classify_http_error,
    classify_sse_error_event,
    classify_stream_eof,
)
from xana.llm_driver.interfaces import CredentialResolver, WireTransport
from xana.llm_driver.transports.dsml_recovery import (
    DSML_FENCE_TAGS,
    contains_dsml_tool_calls,
    parse_dsml_tool_calls,
)
from xana.llm_driver.transports.stream_scrubber import StreamingContextScrubber

_logger = logging.getLogger(__name__)

_DEEPSEEK_EFFORT_MAP: dict[str, str] = {
    "low": "high",
    "medium": "high",
    "high": "high",
    "max": "max",
    "xhigh": "max",
}

_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def build_anthropic_messages_body(
    request: ModelRequest,
    profile: ProviderProfile,
) -> dict[str, Any]:
    """Serialize ModelRequest to Anthropic `/v1/messages` wire JSON."""
    for msg in request.messages:
        if msg.media:
            supported = profile.resolved_modalities(request.model)
            for item in msg.media:
                if item.kind not in supported:
                    raise ValueError(f"模型 {request.model} 不支持 {item.kind} 输入")

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
        elif msg.role == "user":
            _flush_pending()
            if history and history[-1]["role"] == "user":
                prev = history[-1]["content"]
                blocks = prev if isinstance(prev, list) else [{"type": "text", "text": prev}]
                history[-1]["content"] = blocks + [{"type": "text", "text": msg.content}]
            else:
                history.append({"role": "user", "content": msg.content})
        elif msg.role == "assistant":
            _flush_pending()
            tool_calls = (msg.metadata or {}).get("tool_calls") or []
            if tool_calls:
                blocks = []
                reasoning = (msg.metadata or {}).get("reasoning_content")
                if reasoning:
                    blocks.append({"type": "thinking", "thinking": reasoning})
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                for tc in tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("call_id", ""),
                        "name": tc.get("tool_name", ""),
                        "input": tc.get("input", {}) or {},
                    })
                history.append({"role": "assistant", "content": blocks})
            else:
                history.append({"role": "assistant", "content": msg.content})
        elif msg.role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": (msg.metadata or {}).get("tool_use_id", ""),
                "content": msg.content,
            }
            if (msg.metadata or {}).get("ok") is False:
                block["is_error"] = True
            pending_tool_results.append(block)
    _flush_pending()

    body: dict[str, Any] = {
        "model": request.model,
        "max_tokens": profile.resolved_max_tokens(request.model, request.max_tokens),
        "messages": history,
        "stream": True,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if request.tools_schema:
        body["tools"] = [dict(t) for t in request.tools_schema]
    if profile.supports_native_web_search and (request.metadata or {}).get("web_search_native"):
        body.setdefault("tools", []).append(
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
        )
    thinking = (request.metadata or {}).get("thinking")
    if profile.thinking_param_style == "anthropic" and thinking in ("disabled", "enabled"):
        body["thinking"] = {"type": thinking}
    if request.effort and profile.supported_efforts:
        effort = _DEEPSEEK_EFFORT_MAP.get(str(request.effort).lower())
        if effort and effort in profile.supported_efforts:
            body["output_config"] = {"effort": effort}
        else:
            _logger.warning("未知 effort %r,跳过 output_config", request.effort)
    return body


def _merge_usage(acc: dict[str, int], usage: dict[str, Any] | None) -> None:
    if not usage:
        return
    for key in _USAGE_KEYS:
        val = usage.get(key)
        if val is not None:
            acc[key] = int(val)


async def _iter_anthropic_sse_frames(resp: httpx.Response) -> AsyncGenerator[tuple[str, str], None]:
    """Yield `(event_type, data_str)` SSE frames delimited by blank lines."""
    event_type = ""
    data_lines: list[str] = []
    async for raw_line in resp.aiter_lines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                yield event_type, "\n".join(data_lines)
                data_lines.clear()
            event_type = ""
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        yield event_type, "\n".join(data_lines)


def _materialize_tool_block(tb: dict[str, Any]) -> dict[str, Any]:
    raw_json = "".join(tb["parts"]).strip()
    payload: dict[str, Any] = {
        "call_id": tb["call_id"],
        "tool_name": tb["tool_name"],
        "input": {},
    }
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                payload["input"] = parsed
            else:
                payload["input_error"] = f"tool arguments must be a JSON object: {raw_json[:200]}"
        except json.JSONDecodeError as exc:
            payload["input_error"] = f"invalid tool JSON ({exc.msg}): {raw_json[:200]}"
    elif tb["initial_input"]:
        payload["input"] = tb["initial_input"]
    return payload


async def parse_anthropic_sse_stream(
    resp: httpx.Response,
    *,
    enable_dsml_recovery: bool = False,
    request_id: str | None = None,
) -> AsyncGenerator[ModelEvent, None]:
    """Parse Anthropic SSE stream -> ModelEvent sequence with optional DSML recovery."""
    usage_acc: dict[str, int] = {}
    stop_reason: str | None = None
    saw_stop = False
    msg_id = request_id
    tool_blocks: dict[int, dict[str, Any]] = {}
    emitted_tool_calls = 0
    text_buffer: list[str] = []
    scrubber = StreamingContextScrubber(DSML_FENCE_TAGS) if enable_dsml_recovery else None

    async for frame_event_type, data_str in _iter_anthropic_sse_frames(resp):
        stripped = data_str.strip()
        if not stripped:
            continue
        if stripped == "[DONE]":
            saw_stop = True
            break
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue

        ev_type = str(data.get("type") or frame_event_type)
        if ev_type == "error":
            err_obj = data.get("error") if isinstance(data.get("error"), dict) else data
            yield ModelEvent(kind="error", error=classify_sse_error_event(err_obj, request_id=msg_id))
            return
        if ev_type == "message_start":
            msg = data.get("message") or {}
            if isinstance(msg, dict):
                if msg.get("id"):
                    msg_id = str(msg["id"])
                _merge_usage(usage_acc, msg.get("usage"))
        elif ev_type == "content_block_start":
            idx = int(data.get("index") or 0)
            cb = data.get("content_block") or {}
            if isinstance(cb, dict) and cb.get("type") == "tool_use":
                tool_blocks[idx] = {
                    "call_id": str(cb.get("id") or f"tool_{idx}"),
                    "tool_name": str(cb.get("name") or ""),
                    "parts": [],
                    "initial_input": cb.get("input") if isinstance(cb.get("input"), dict) else None,
                }
        elif ev_type == "content_block_delta":
            idx = int(data.get("index") or 0)
            delta = data.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            d_type = str(delta.get("type") or "")
            if d_type == "thinking_delta":
                thinking_text = delta.get("thinking") or delta.get("reasoning_content") or ""
                if thinking_text:
                    yield ModelEvent(kind="text_delta", payload={"text": str(thinking_text), "channel": "thinking"})
            elif d_type == "text_delta":
                raw_text = str(delta.get("text") or "")
                if raw_text:
                    text_buffer.append(raw_text)
                    visible = scrubber.feed(raw_text) if scrubber is not None else raw_text
                    if visible:
                        yield ModelEvent(kind="text_delta", payload={"text": visible})
            elif d_type == "input_json_delta":
                if idx in tool_blocks:
                    tool_blocks[idx]["parts"].append(str(delta.get("partial_json") or ""))
            else:
                thinking_text = delta.get("thinking") or delta.get("reasoning_content")
                if thinking_text:
                    yield ModelEvent(kind="text_delta", payload={"text": str(thinking_text), "channel": "thinking"})
                text_val = delta.get("text")
                if text_val:
                    raw_text = str(text_val)
                    text_buffer.append(raw_text)
                    visible = scrubber.feed(raw_text) if scrubber is not None else raw_text
                    if visible:
                        yield ModelEvent(kind="text_delta", payload={"text": visible})
        elif ev_type == "content_block_stop":
            idx = int(data.get("index") or 0)
            tb = tool_blocks.pop(idx, None)
            if tb is not None:
                emitted_tool_calls += 1
                yield ModelEvent(kind="tool_call", payload=_materialize_tool_block(tb))
        elif ev_type == "message_delta":
            delta = data.get("delta") or {}
            if isinstance(delta, dict) and delta.get("stop_reason"):
                sr = str(delta["stop_reason"])
                stop_reason = "length" if sr == "max_tokens" else sr
            _merge_usage(usage_acc, data.get("usage"))
        elif ev_type == "message_stop":
            saw_stop = True
            break

    # CRITICAL: check mid-stream EOF BEFORE flushing scrubber or running DSML recovery!
    if not saw_stop and stop_reason is None:
        yield ModelEvent(kind="error", error=classify_stream_eof())
        return

    if scrubber is not None:
        tail = scrubber.flush()
        if tail:
            yield ModelEvent(kind="text_delta", payload={"text": tail})

    # Materialize any tool_use block that lacked content_block_stop before message_stop
    for idx in sorted(tool_blocks):
        emitted_tool_calls += 1
        yield ModelEvent(kind="tool_call", payload=_materialize_tool_block(tool_blocks[idx]))

    if enable_dsml_recovery and emitted_tool_calls == 0:
        full_text = "".join(text_buffer)
        if contains_dsml_tool_calls(full_text):
            _, recovered = parse_dsml_tool_calls(full_text)
            if recovered:
                _logger.warning("DSML_LEAK_WORKAROUND: recovered %d leaked tool call(s)", len(recovered))
                for call in recovered:
                    yield ModelEvent(
                        kind="tool_call",
                        payload={
                            "call_id": f"dsml-recovered-{uuid.uuid4().hex[:12]}",
                            "tool_name": call["name"],
                            "input": call["input"],
                        },
                    )
                stop_reason = "tool_use"

    if usage_acc:
        yield ModelEvent(
            kind="usage",
            usage=ModelUsage(
                input_tokens=usage_acc.get("input_tokens"),
                output_tokens=usage_acc.get("output_tokens"),
                cache_read_tokens=usage_acc.get("cache_read_input_tokens"),
                cache_write_tokens=usage_acc.get("cache_creation_input_tokens"),
            ),
        )
    done_payload: dict[str, Any] = {"stop_reason": stop_reason or "end_turn"}
    if msg_id:
        done_payload["request_id"] = msg_id
    yield ModelEvent(kind="done", payload=done_payload)


class AnthropicMessagesTransport(WireTransport):
    """Stateless single-attempt Anthropic `/v1/messages` wire transport."""

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._injected_client = http_client
        self._owned_client: httpx.AsyncClient | None = None

    @property
    def dialect(self) -> str:
        return "anthropic_messages"

    def _client(self, profile: ProviderProfile) -> httpx.AsyncClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._owned_client is None:
            read_timeout = profile.idle_timeout_s if profile.idle_timeout_s is not None else profile.timeout_s
            self._owned_client = httpx.AsyncClient(
                timeout=httpx.Timeout(profile.timeout_s, connect=30.0, read=read_timeout),
                trust_env=profile.trust_env,
            )
        return self._owned_client

    async def stream_attempt(
        self,
        request: ModelRequest,
        profile: ProviderProfile,
        credentials: CredentialResolver,
    ) -> AsyncGenerator[ModelEvent, None]:
        try:
            body = build_anthropic_messages_body(request, profile)
        except ValueError as exc:
            yield ModelEvent(
                kind="error",
                error=ProviderError(kind="bad_request", message=str(exc), retryable=False, reason="bad_request"),
            )
            return
        try:
            auth_headers = await credentials.auth_headers()
        except Exception as exc:
            yield ModelEvent(
                kind="error",
                error=ProviderError(kind="auth", message=str(exc), retryable=False, reason="auth"),
            )
            return
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "anthropic-version": profile.anthropic_version,
            **profile.default_headers,
            **auth_headers,
        }
        url = profile.base_url.rstrip("/") + "/v1/messages"
        client = self._client(profile)
        req_id: str | None = None
        try:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                req_id = resp.headers.get("request-id") or resp.headers.get("x-request-id")
                if resp.status_code >= 400:
                    err_body = (await resp.aread()).decode("utf-8", errors="replace")
                    yield ModelEvent(
                        kind="error",
                        error=classify_http_error(resp.status_code, err_body, headers=resp.headers, request_id=req_id),
                    )
                    return
                async for evt in parse_anthropic_sse_stream(
                    resp,
                    enable_dsml_recovery=profile.enable_dsml_recovery,
                    request_id=req_id,
                ):
                    yield evt
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield ModelEvent(kind="error", error=classify_exception(exc, request_id=req_id))

    def capabilities(self, profile: ProviderProfile, model: str | None = None) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=profile.name,
            supports_streaming=True,
            supports_tools=True,
            supports_parallel_tools=profile.supports_parallel_tools,
            supports_cache_control=profile.supports_cache_control,
            supports_json_schema=False,
            max_context_tokens=profile.default_context_window,
            max_output_tokens=profile.resolved_max_tokens(model or "", None),
            input_modalities=profile.resolved_modalities(model),
        )

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None
