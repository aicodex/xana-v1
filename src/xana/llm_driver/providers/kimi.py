"""Kimi(k3 / kimi-for-coding)via OpenAI chat-completions 方言。

kimi-code 的托管模型默认走 kimi 协议(OpenAI 方言,基于 openai SDK),
base ``https://api.kimi.com/coding/v1``(mainland)/ ``api.kimi.ai``(global);
模型列表由服务端 ``GET /models`` 声明(k3 与 kimi-for-coding 均 1M 上下文)。
本客户端复刻 kimi-code packages/kosong/src/providers/kimi.ts 的关键行为:

  * 只发 ``max_completion_tokens``,不发 ``max_tokens``(reasoning 模型上
    ``max_tokens`` 与 reasoning_content 共享预算,给小了会 200 空 content)。
  * thinking 走 body 级 ``thinking: {"type": "enabled", "effort"?}``;
    请求方要求 disabled 时省略该字段(k3/kimi-for-coding 是 thinking-only,
    省略比显式 disabled 更稳妥;调用方本来就不把 reasoning 通道计入结果)。
  * 保留思考(preserved thinking):thinking 开启时发 ``thinking.keep="all"``,
    且**每条** assistant 历史都带 ``reasoning_content``(无则空串)——官方要求
    原样回传,K3 博客称不回传会使输出高度不稳定。
  * assistant 有 tool_calls 且文本为空时省略 ``content`` 字段;tool_call_id
    规整为 ``[A-Za-z0-9_-]`` 且 ≤64 字符(kosong KIMI_TOOL_CALL_ID_POLICY)。
  * 不发 temperature/top_p 等采样参数(服务端固定,传其他值报错)。
  * ``request.metadata["prompt_cache_key"]``(harness 填 session id)→
    ``prompt_cache_key``,提升前缀缓存命中。
  * 429 / 5xx / 网络错误在尚未产出任何事件前指数退避重试;耗尽后 429 以
    ``error(kind="rate_limit")`` 上抛,调用方可见。
  * 401 → 强制刷新 token 重试一次;再 401 报 auth 错误引导 ``xana login``。
  * tool_call arguments 不是合法 JSON 时,payload 带 ``input_error``,由 harness
    回给模型一条失败结果,而不是静默以 ``{}`` 执行。

Auth: ``Authorization: Bearer <oauth access_token>``(见 kimi_oauth)。

Wire shape(同 xana ``ModelEventKind``):
  text_delta  ← delta.content;reasoning 走 payload["channel"]=="thinking"
  tool_call   ← finish_reason=="tool_calls" 时按 index 聚合的 delta.tool_calls
  error       ← HTTP 4xx/5xx(429 重试耗尽为 kind="rate_limit")、网络错误
  usage       ← 流尾 usage(stream_options.include_usage)
  done        ← 流结束(成功时永远最后)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
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
)
from xana.llm_driver.interfaces import ModelClient
from xana.llm_driver.providers.kimi_oauth import KimiAuthError, KimiTokenStore

_logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
_DEFAULT_MAX_TOKENS = 32768  # 含 reasoning;官方建议工具调用 ≥16k
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 1.0
_TOOL_CALL_ID_MAX = 64
_TOOL_CALL_ID_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")

SUPPORTED_EFFORTS = ("low", "high", "max")  # k3/kimi-for-coding think_efforts.valid_efforts
_EFFORT_MAP = {"low": "low", "medium": "high", "high": "high",
               "xhigh": "max", "max": "max"}


@dataclass(frozen=True)
class KimiConfig:
    base_url: str = _DEFAULT_BASE_URL
    max_tokens: int = _DEFAULT_MAX_TOKENS
    timeout_s: float = 60.0
    backoff_base_s: float = _BACKOFF_BASE_S


class KimiClient(ModelClient):
    """ModelClient for Kimi over the OpenAI chat-completions dialect.

    token 走 KimiTokenStore(到期自动刷新);httpx.AsyncClient 懒创建,
    测试可经 ``http_client`` 注入 MockTransport。
    """

    def __init__(
        self,
        config: KimiConfig,
        *,
        token_store: KimiTokenStore,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._store = token_store
        self._http = http_client
        self._owns_http = http_client is None

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        try:
            token = await self._store.access_token()
        except KimiAuthError as exc:
            yield _auth_error(str(exc))
            return
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            emitted = False
            retry: ModelEvent | None = None
            async with contextlib.aclosing(self._stream_once(request, token)) as events:
                async for event in events:
                    err = event.error if event.kind == "error" else None
                    if err is not None and not emitted:
                        if err.kind == "auth" and err.raw_status == 401 and not refreshed:
                            retry = event
                            break
                        if err.retryable and attempt < _MAX_ATTEMPTS:
                            retry = event
                            break
                    emitted = True
                    yield event
            if retry is None:
                return
            err = retry.error
            assert err is not None
            if err.kind == "auth":
                # 401 → 强制刷新后重试一次(kimi-code 同款);第二个 401 才上抛
                refreshed = True
                try:
                    token = await self._store.access_token(force_refresh=True)
                except KimiAuthError as exc:
                    yield _auth_error(str(exc))
                    return
                continue
            delay = err.raw.get("retry_after_s")
            if delay is None:
                delay = self._config.backoff_base_s * (2 ** (attempt - 1))
            _logger.warning("kimi 请求失败(%s),%.1fs 后重试 %d/%d",
                            err.message[:120], delay, attempt, _MAX_ATTEMPTS - 1)
            await asyncio.sleep(delay)

    async def _stream_once(
        self, request: ModelRequest, token: str
    ) -> AsyncGenerator[ModelEvent, None]:
        body = _build_request_body(request, self._config)
        url = self._config.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        http = self._http or httpx.AsyncClient()
        try:
            try:
                async with http.stream(
                    "POST", url, json=body, headers=headers, timeout=self._config.timeout_s
                ) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        yield ModelEvent(
                            kind="error",
                            error=ProviderError(
                                kind=_http_error_kind(resp.status_code),
                                message=_error_message(resp),
                                retryable=resp.status_code == 429 or resp.status_code >= 500,
                                raw_status=resp.status_code,
                                raw=_retry_after_raw(resp),
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
            provider="kimi",
            supports_streaming=True,
            supports_tools=True,
            supports_cache_control=False,
            max_output_tokens=self._config.max_tokens,
        )

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()


def _auth_error(message: str) -> ModelEvent:
    return ModelEvent(
        kind="error",
        error=ProviderError(kind="auth", message=message, retryable=False, raw_status=401),
    )


def _error_message(resp: httpx.Response) -> str:
    base = f"HTTP {resp.status_code}: {resp.text[:300]}"
    if resp.status_code == 401:
        return base + "(kimi token 失效;如刷新后仍 401,请重新运行 `xana login`)"
    return base


def _retry_after_raw(resp: httpx.Response) -> dict[str, Any]:
    raw = resp.headers.get("retry-after")
    try:
        return {"retry_after_s": float(raw)} if raw else {}
    except ValueError:
        return {}


def _normalize_tool_call_id(call_id: str) -> str:
    return _TOOL_CALL_ID_UNSAFE.sub("_", call_id)[:_TOOL_CALL_ID_MAX] or "tool_call"


def _http_error_kind(status: int) -> ProviderErrorKind:
    if status == 429:
        return "rate_limit"
    if status in (401, 403):
        return "auth"
    if status in (400, 422):
        return "bad_request"
    if status >= 500:
        return "server_error"
    return "unknown"


def _convert_tools(tools_schema: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Anthropic-shape {name, description, input_schema} → OpenAI function tools。"""
    out: list[dict[str, Any]] = []
    for t in tools_schema:
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return out


def _build_request_body(request: ModelRequest, config: KimiConfig) -> dict[str, Any]:
    """ModelMessages → OpenAI chat-completions wire shape。

    assistant 的 metadata['tool_calls'] 渲染为 OpenAI tool_calls 数组
    (arguments 是 JSON 字符串);role='tool' 消息一对一映射为
    {"role": "tool", "tool_call_id", "content"}(OpenAI 允许连续 tool 消息,
    无需像 Anthropic 方言那样合并)。
    """
    thinking = (request.metadata or {}).get("thinking")
    effort = _EFFORT_MAP.get(str(request.effort).lower()) if request.effort else None
    thinking_on = thinking == "enabled" or bool(effort)
    history: list[dict[str, Any]] = []
    for msg in request.messages:
        if msg.role in ("system", "user"):
            history.append({"role": msg.role, "content": msg.content})
            continue
        if msg.role == "assistant":
            out: dict[str, Any] = {"role": "assistant"}
            tool_calls = (msg.metadata or {}).get("tool_calls") or []
            # 取消/仅思考的轮次可留在历史中，但 Kimi 不接受没有回答或工具调用的 assistant。
            if not msg.content and not tool_calls:
                continue
            if msg.content or not tool_calls:
                out["content"] = msg.content or ""
            reasoning = (msg.metadata or {}).get("reasoning_content")
            if reasoning or thinking_on:
                out["reasoning_content"] = reasoning or ""
            if tool_calls:
                out["tool_calls"] = [
                    {
                        "id": _normalize_tool_call_id(tc.get("call_id", "")),
                        "type": "function",
                        "function": {
                            "name": tc.get("tool_name", ""),
                            "arguments": json.dumps(tc.get("input") or {}, ensure_ascii=False),
                        },
                    }
                    for tc in tool_calls
                ]
            history.append(out)
            continue
        if msg.role == "tool":
            history.append({
                "role": "tool",
                "tool_call_id": _normalize_tool_call_id((msg.metadata or {}).get("tool_use_id", "")),
                "content": msg.content,
            })
            continue

    body: dict[str, Any] = {
        "model": request.model,
        "messages": history,
        "stream": True,
        "stream_options": {"include_usage": True},
        # kimi.ts: 只发 max_completion_tokens — max_tokens 与 reasoning 共享预算
        "max_completion_tokens": config.max_tokens,
    }
    if request.tools_schema:
        body["tools"] = _convert_tools(request.tools_schema)
    cache_key = (request.metadata or {}).get("prompt_cache_key")
    if cache_key:
        body["prompt_cache_key"] = str(cache_key)
    # thinking:k3/kimi-for-coding 是 thinking-only;enabled 显式开,
    # disabled 省略字段(服务端维持默认),不在 compact 场景硬关。
    if thinking_on:
        body["thinking"] = {"type": "enabled", "keep": "all",
                            **({"effort": effort} if effort else {})}
    elif thinking == "disabled":
        _logger.debug("kimi thinking-only 模型:忽略 thinking=disabled,省略 thinking 字段")
    if request.effort and not effort:
        _logger.warning("未知 effort %r,跳过 thinking.effort", request.effort)
    return body


async def _parse_sse_stream(resp: httpx.Response) -> AsyncGenerator[ModelEvent, None]:
    """OpenAI SSE → ModelEvent 序列。

    delta.tool_calls 按 index 聚合(id/name 只在首片出现,arguments 分片),
    finish_reason=="tool_calls" 时一次性发出;流尾 usage → usage,然后 done。
    """
    tool_blocks: dict[int, dict[str, str]] = {}
    usage_payload: dict[str, Any] | None = None
    tool_calls_finished = False
    finish_reason: str | None = None
    data_lines: list[str] = []

    async def _handle(payload: dict[str, Any]) -> AsyncGenerator[ModelEvent, None]:
        nonlocal usage_payload, tool_calls_finished, finish_reason
        if payload.get("usage"):
            usage_payload = payload["usage"]
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                yield ModelEvent(
                    kind="text_delta",
                    payload={"text": reasoning, "channel": "thinking"},
                )
            content = delta.get("content")
            if content:
                yield ModelEvent(kind="text_delta", payload={"text": content})
            for tc in delta.get("tool_calls") or []:
                idx = int(tc.get("index", 0))
                block = tool_blocks.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    block["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    block["name"] = fn["name"]
                if fn.get("arguments"):
                    block["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
            if choice.get("finish_reason") == "tool_calls":
                tool_calls_finished = True

    async for raw_line in resp.aiter_lines():
        if raw_line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                data_lines = []
                if raw.strip() == "[DONE]":
                    break
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    async for ev in _handle(payload):
                        yield ev
            continue
        if raw_line.startswith(":"):
            continue
        if raw_line.startswith("data:"):
            data_lines.append(raw_line[len("data:"):].lstrip())

    if data_lines:
        raw = "\n".join(data_lines)
        if raw.strip() != "[DONE]":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                async for ev in _handle(payload):
                    yield ev

    if tool_calls_finished or tool_blocks:
        for idx in sorted(tool_blocks):
            block = tool_blocks[idx]
            payload: dict[str, Any] = {"tool_name": block["name"], "call_id": block["id"]}
            try:
                tool_input = json.loads(block["arguments"] or "{}")
            except json.JSONDecodeError as exc:
                tool_input = None
                payload["input_error"] = (
                    f"arguments 不是合法 JSON({exc.msg}): {block['arguments'][:200]}")
            if tool_input is not None and not isinstance(tool_input, dict):
                payload["input_error"] = f"arguments 必须是 JSON 对象: {block['arguments'][:200]}"
                tool_input = None
            payload["input"] = tool_input or {}
            yield ModelEvent(kind="tool_call", payload=payload)

    usage_payload = usage_payload or {}
    yield ModelEvent(
        kind="usage",
        usage=ModelUsage(
            input_tokens=usage_payload.get("prompt_tokens"),
            output_tokens=usage_payload.get("completion_tokens"),
            cache_read_tokens=(usage_payload.get("prompt_tokens_details") or {}).get("cached_tokens"),
        ),
    )
    # stop_reason 归一:length = 输出被 max tokens 截断(harness 据此续写)
    yield ModelEvent(kind="done", payload={"stop_reason": finish_reason or ""})
