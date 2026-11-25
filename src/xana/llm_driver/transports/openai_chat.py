"""OpenAI Chat Completions wire dialect transport.

Shared by Kimi OAuth, OpenAI-Compatible endpoints, OpenRouter, vLLM, Ollama, etc.
Vendor differences are driven strictly by `ProviderProfile` — zero vendor subclassing.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, AsyncGenerator

import httpx

from xana.llm_driver.accumulator import ToolCallAccumulator, sanitize_tool_call_id
from xana.llm_driver.auth.kimi_media import KimiMedia
from xana.llm_driver.auth.kimi_oauth import KimiAuthError
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
from xana.multimodal.files import read_media_bytes

_logger = logging.getLogger(__name__)

_KIMI_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "high",
    "high": "high",
    "max": "max",
    "xhigh": "max",
}


def _convert_tools(tools_schema: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools_schema
    ]


def _extract_usage(payload: dict[str, Any]) -> ModelUsage:
    """OpenAI prompt_tokens includes cached_tokens; normalize input_tokens to uncached."""
    prompt = payload.get("prompt_tokens")
    cached = payload.get("cached_tokens")
    if cached is None:
        cached = (payload.get("prompt_tokens_details") or {}).get("cached_tokens")
    reasoning = (payload.get("completion_tokens_details") or {}).get("reasoning_tokens")
    return ModelUsage(
        input_tokens=max(0, prompt - (cached or 0)) if prompt is not None else None,
        output_tokens=payload.get("completion_tokens"),
        cache_read_tokens=cached,
        reasoning_tokens=reasoning,
        total_tokens=payload.get("total_tokens"),
    )


def build_openai_chat_body(
    request: ModelRequest,
    profile: ProviderProfile,
    *,
    media_parts: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Serialize ModelRequest to OpenAI Chat Completions wire JSON per ProviderProfile."""
    thinking = (request.metadata or {}).get("thinking")
    effort: str | None = None
    if profile.thinking_param_style == "kimi" and request.effort:
        effort = _KIMI_EFFORT_MAP.get(str(request.effort).lower())
        if not effort:
            _logger.warning("未知 effort %r,跳过 thinking.effort", request.effort)
    thinking_on = (
        profile.thinking_param_style == "kimi"
        and (thinking == "enabled" or bool(effort))
    )

    history: list[dict[str, Any]] = []
    for idx, msg in enumerate(request.messages):
        content: Any = msg.content
        if msg.media:
            if msg.role not in ("user", "tool"):
                raise ValueError("媒体输入只能来自 user 或 tool 消息")
            if media_parts is None or idx not in media_parts:
                raise ValueError("media inputs must be prepared before serialization")
            content = ([{"type": "text", "text": msg.content}] if msg.content else []) + media_parts[idx]

        if msg.role in ("system", "user"):
            history.append({"role": msg.role, "content": content})
        elif msg.role == "assistant":
            tool_calls = (msg.metadata or {}).get("tool_calls") or []
            if not msg.content and not tool_calls:
                continue
            out: dict[str, Any] = {"role": "assistant"}
            if msg.content or not tool_calls:
                out["content"] = msg.content or ""
            reasoning = (msg.metadata or {}).get("reasoning_content")
            if profile.preserve_thinking_history and (reasoning or thinking_on):
                out["reasoning_content"] = reasoning or ""
            if tool_calls:
                out["tool_calls"] = [
                    {
                        "id": sanitize_tool_call_id(tc.get("call_id", ""), i),
                        "type": "function",
                        "function": {
                            "name": tc.get("tool_name", ""),
                            "arguments": json.dumps(tc.get("input") or {}, ensure_ascii=False),
                        },
                    }
                    for i, tc in enumerate(tool_calls)
                ]
            history.append(out)
        elif msg.role == "tool":
            history.append({
                "role": "tool",
                "tool_call_id": sanitize_tool_call_id((msg.metadata or {}).get("tool_use_id", "")),
                "content": content,
            })

    body: dict[str, Any] = {
        "model": request.model,
        "messages": history,
        "stream": True,
        "stream_options": {"include_usage": True},
        profile.max_tokens_param: profile.resolved_max_tokens(request.model, request.max_tokens),
    }
    if request.tools_schema:
        body["tools"] = _convert_tools(request.tools_schema)
    if profile.supports_prompt_cache_key:
        cache_key = (request.metadata or {}).get("prompt_cache_key")
        if cache_key:
            body["prompt_cache_key"] = str(cache_key)
    if thinking_on:
        body["thinking"] = {
            "type": "enabled",
            "keep": "all",
            **({"effort": effort} if effort else {}),
        }
    return body


async def _iter_sse_data_frames(resp: httpx.Response) -> AsyncGenerator[str, None]:
    """Yield assembled SSE `data:` payloads delimited by blank lines (handles multi-line data)."""
    data_lines: list[str] = []
    async for raw_line in resp.aiter_lines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        yield "\n".join(data_lines)


async def parse_openai_sse_stream(
    resp: httpx.Response,
    *,
    request_id: str | None = None,
) -> AsyncGenerator[ModelEvent, None]:
    """Parse OpenAI Chat Completions SSE stream -> ModelEvent sequence.

    Contract invariant: mid-stream EOF without [DONE] or finish_reason emits
    `error(reason="stream_eof", retryable=True)`, never silently succeeding.
    """
    acc = ToolCallAccumulator()
    finish_reason: str | None = None
    saw_done = False
    usage_emitted: ModelUsage | None = None

    async for data_str in _iter_sse_data_frames(resp):
        stripped = data_str.strip()
        if not stripped:
            continue
        if stripped == "[DONE]":
            saw_done = True
            break
        try:
            chunk = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if "error" in chunk and isinstance(chunk["error"], dict):
            yield ModelEvent(kind="error", error=classify_sse_error_event(chunk["error"], request_id=request_id))
            return

        if isinstance(chunk.get("usage"), dict):
            usage_emitted = _extract_usage(chunk["usage"])

        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if isinstance(delta, dict):
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                yield ModelEvent(kind="text_delta", payload={"text": reasoning, "channel": "thinking"})
            content = delta.get("content")
            if content:
                yield ModelEvent(kind="text_delta", payload={"text": content})
            for tc_delta in delta.get("tool_calls") or []:
                if not isinstance(tc_delta, dict):
                    continue
                fn = tc_delta.get("function") or {}
                acc.feed(
                    index=int(tc_delta.get("index") or 0),
                    call_id=tc_delta.get("id"),
                    name=str(fn.get("name") or ""),
                    args_chunk=str(fn.get("arguments") or ""),
                )
        fr = choice.get("finish_reason")
        if fr:
            finish_reason = str(fr)

    if not saw_done and finish_reason is None:
        yield ModelEvent(kind="error", error=classify_stream_eof())
        return

    for tc in acc.materialize():
        yield ModelEvent(kind="tool_call", payload=tc)
    if usage_emitted is not None:
        yield ModelEvent(kind="usage", usage=usage_emitted)
    done_payload: dict[str, Any] = {"stop_reason": finish_reason or "stop"}
    if request_id:
        done_payload["request_id"] = request_id
    yield ModelEvent(kind="done", payload=done_payload)


class OpenAIChatTransport(WireTransport):
    """Stateless single-attempt OpenAI Chat Completions transport."""

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._injected_client = http_client
        self._owned_client: httpx.AsyncClient | None = None
        self._media_cache: dict[str, KimiMedia] = {}

    @property
    def dialect(self) -> str:
        return "openai_chat"

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

    async def _prepare_media(
        self,
        request: ModelRequest,
        profile: ProviderProfile,
        auth_headers: dict[str, str],
    ) -> dict[int, list[dict[str, Any]]]:
        media_parts: dict[int, list[dict[str, Any]]] = {}
        if not any(m.media for m in request.messages):
            return media_parts
        supported = profile.resolved_modalities(request.model)
        token = auth_headers.get("Authorization", "").removeprefix("Bearer ").strip()
        kimi_media: KimiMedia | None = None
        for idx, msg in enumerate(request.messages):
            if not msg.media:
                continue
            if "video" in supported and any(item.kind == "video" for item in msg.media):
                if kimi_media is None:
                    kimi_media = self._media_cache.setdefault(
                        profile.base_url,
                        KimiMedia(profile.base_url, profile.model_input_modalities, self._client(profile)),
                    )
                media_parts[idx] = await kimi_media.parts(msg.media, request.model, token)
            else:
                parts: list[dict[str, Any]] = []
                for item in msg.media:
                    if item.kind not in supported:
                        raise ValueError(f"模型 {request.model} 未在配置中开启 {item.kind} 输入")
                    data = await asyncio.to_thread(read_media_bytes, item)
                    url = f"data:{item.mime_type};base64," + base64.b64encode(data).decode("ascii")
                    parts.append({"type": "image_url", "image_url": {"url": url}})
                media_parts[idx] = parts
        return media_parts

    async def stream_attempt(
        self,
        request: ModelRequest,
        profile: ProviderProfile,
        credentials: CredentialResolver,
    ) -> AsyncGenerator[ModelEvent, None]:
        try:
            auth_headers = await credentials.auth_headers()
        except Exception as exc:
            yield ModelEvent(
                kind="error",
                error=ProviderError(kind="auth", message=str(exc), retryable=False, reason="auth"),
            )
            return
        try:
            media_parts = await self._prepare_media(request, profile, auth_headers)
            body = build_openai_chat_body(request, profile, media_parts=media_parts)
        except httpx.HTTPStatusError as exc:
            resp = exc.response
            req_id = resp.headers.get("x-request-id") or resp.headers.get("request-id")
            yield ModelEvent(
                kind="error",
                error=classify_http_error(resp.status_code, resp.text, headers=resp.headers, request_id=req_id),
            )
            return
        except httpx.HTTPError as exc:
            yield ModelEvent(kind="error", error=classify_exception(exc))
            return
        except (ValueError, OSError) as exc:
            yield ModelEvent(
                kind="error",
                error=ProviderError(kind="bad_request", message=str(exc), retryable=False, reason="bad_request"),
            )
            return
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **profile.default_headers,
            **auth_headers,
        }
        url = profile.base_url.rstrip("/") + "/chat/completions"
        client = self._client(profile)
        req_id: str | None = None
        try:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                req_id = resp.headers.get("x-request-id") or resp.headers.get("request-id")
                if resp.status_code >= 400:
                    err_body = (await resp.aread()).decode("utf-8", errors="replace")
                    yield ModelEvent(
                        kind="error",
                        error=classify_http_error(resp.status_code, err_body, headers=resp.headers, request_id=req_id),
                    )
                    return
                async for evt in parse_openai_sse_stream(resp, request_id=req_id):
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
