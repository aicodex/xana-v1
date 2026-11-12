"""Fake LLM client for tests and offline development.

Behavior depends on whether a `ScriptedTurn` overrides the default:

* **Default turn** (empty `text_deltas` + no `tool_call`) cooperates with
  DefaultHarness's multi-round tool loop:
  - cursor=0: chunk-stream the echoed prompt (DS wire-shape) and emit one
    `tool_call` to `memory_search` derived from the last user message.
  - cursor>=1: stream a post-tool wrap-up ending with the fixed
    `[fake-reply ok]` marker; NO tool_call → the harness loop terminates.

* **Scripted turn**: explicit `text_deltas` / `tool_call` produce that exact
  sequence. Once the scripted list is exhausted (`cursor >= len(turns)`)
  the client returns a no-op `usage` + `done` so any subsequent harness
  iteration ends cleanly instead of looping forever on the last script.

`InMemoryModelRegistry` optionally wraps per-provider clients in a
semaphore guard driven by `concurrency_limits` (§4.3: 消费
SystemConfig.limit().max_concurrent,第二个并发请求排队)。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncGenerator, AsyncIterator

from xana.llm_driver.contracts import ModelEvent, ModelMessage, ModelRequest, ModelUsage, ProviderCapabilities
from xana.llm_driver.interfaces import ModelClient, ModelRegistry


_FAKE_REPLY_MARKER = "[fake-reply ok]"
_DEFAULT_TOOL_NAME = "memory_search"
_DEFAULT_TOOL_CALL_ID = "fake-call-1"
_DEFAULT_TOOL_TOP_K = 3


@dataclass(frozen=True)
class ScriptedTurn:
    # Each delta is either a plain string (payload {"text": s}) or a dict
    # payload with extra keys (e.g. {"text": "…", "channel": "thinking"}).
    text_deltas: tuple[str | dict[str, Any], ...] = ()
    tool_call: tuple[str, dict[str, Any], str] | None = None  # (tool_name, input, call_id)
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0


def _echo_prompt_body(messages: tuple[ModelMessage, ...]) -> str:
    parts = ["[fake-reply] echoed prompt:"]
    for msg in messages:
        parts.append(f"--- {msg.role} ---")
        parts.append(msg.content)
    parts.append("--- /prompt ---")
    return "\n".join(parts) + "\n"


def _last_user_text(messages: tuple[ModelMessage, ...]) -> str:
    for msg in reversed(messages):
        if msg.role == "user" and msg.content.strip() and not msg.metadata.get("note_kind"):
            return msg.content
    return "(no user input)"


def _last_tool_result(messages: tuple[ModelMessage, ...]) -> str:
    for msg in reversed(messages):
        if msg.role == "tool" and msg.content:
            return msg.content
    return "(no tool result on history)"


def _chunk_lines(text: str) -> list[str]:
    """Split text into line-sized chunks, preserving terminators.

    Each chunk goes out as its own `text_delta` event — mirrors how the real
    DeepSeek client surfaces SSE `content_block_delta` payloads one at a time.
    """
    return text.splitlines(keepends=True) or [text]


class FakeModelClient(ModelClient):
    def __init__(self, *turns: ScriptedTurn) -> None:
        self._turns: list[ScriptedTurn] = list(turns) or [ScriptedTurn()]
        self._cursor = 0

    def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        cursor = self._cursor
        self._cursor += 1

        turn = self._turns[min(cursor, len(self._turns) - 1)]
        is_default_turn = not turn.text_deltas and turn.tool_call is None

        # Default turn handles its own cursor-based round logic (echo+tool
        # on round 0, wrap-up on round >=1) — let it own that decision.
        if is_default_turn:
            async for event in self._default_turn_events(request, turn, cursor):
                yield event
            return

        # Explicit scripted turns: once the list is exhausted, return a no-op
        # so DefaultHarness's outer loop terminates instead of replaying the
        # last script forever. NO usage event — the prior round's tally
        # should stay (a zero-value emit here would clobber it).
        if cursor >= len(self._turns):
            yield ModelEvent(kind="done")
            return

        for delta in turn.text_deltas:
            payload = dict(delta) if isinstance(delta, dict) else {"text": delta}
            yield ModelEvent(kind="text_delta", payload=payload)
        if turn.tool_call is not None:
            name, input_, call_id = turn.tool_call
            yield ModelEvent(
                kind="tool_call",
                payload={"tool_name": name, "input": input_, "call_id": call_id},
            )
        yield ModelEvent(
            kind="usage",
            usage=ModelUsage(
                input_tokens=turn.usage_input_tokens,
                output_tokens=turn.usage_output_tokens,
            ),
        )
        yield ModelEvent(kind="done")

    async def _default_turn_events(
        self, request: ModelRequest, turn: ScriptedTurn, cursor: int
    ) -> AsyncGenerator[ModelEvent, None]:
        if cursor == 0:
            # Round 0: echo the assembled prompt + fire one tool_call to
            # memory_search. No marker yet — the harness must run the tool and
            # invoke us again with the tool result before we conclude.
            for chunk in _chunk_lines(_echo_prompt_body(request.messages)):
                yield ModelEvent(kind="text_delta", payload={"text": chunk})
            query = _last_user_text(request.messages)
            yield ModelEvent(
                kind="tool_call",
                payload={
                    "tool_name": _DEFAULT_TOOL_NAME,
                    "input": {"query": query, "top_k": _DEFAULT_TOOL_TOP_K},
                    "call_id": _DEFAULT_TOOL_CALL_ID,
                },
            )
        else:
            # Round >=1: harness re-entered us with the tool result on the
            # history. Wrap up with the marker, no further tool_calls — the
            # outer loop then exits.
            tool_result = _last_tool_result(request.messages)
            wrapup = (
                f"[fake-reply] saw {_DEFAULT_TOOL_NAME} result "
                f"({tool_result[:60]!r}); concluding.\n{_FAKE_REPLY_MARKER}\n"
            )
            for chunk in _chunk_lines(wrapup):
                yield ModelEvent(kind="text_delta", payload={"text": chunk})

        yield ModelEvent(
            kind="usage",
            usage=ModelUsage(
                input_tokens=turn.usage_input_tokens,
                output_tokens=turn.usage_output_tokens,
            ),
        )
        yield ModelEvent(kind="done")

    async def cancel(self, request_id: str) -> None:
        return None

    async def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        return ProviderCapabilities(provider="fake", supports_streaming=True, supports_tools=True)


class _ConcurrencyGuardedClient(ModelClient):
    """per-provider semaphore 包装(§4.3):同 provider 并发 stream 受
    max_concurrent 限制,超出的请求排队等待空闲并发位。"""

    def __init__(self, inner: ModelClient, semaphore: asyncio.Semaphore) -> None:
        self._inner = inner
        self._semaphore = semaphore

    async def _guarded_stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        async with self._semaphore:
            async for event in self._inner.stream(request):
                yield event

    def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        return self._guarded_stream(request)

    async def cancel(self, request_id: str) -> None:
        await self._inner.cancel(request_id)

    async def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        return await self._inner.capabilities(model=model)


class InMemoryModelRegistry(ModelRegistry):
    def __init__(self, concurrency_limits: dict[str, int] | None = None) -> None:
        self._clients: dict[str, ModelClient] = {}
        # §4.3: provider → max_concurrent(SystemConfig.limit() 的消费者)。
        # None/缺省 = 不限制,直通原始 client(零行为变更)。
        self._concurrency_limits: dict[str, int] = dict(concurrency_limits or {})
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    async def register(self, provider: str, client: ModelClient) -> None:
        self._clients[provider] = client

    async def get(self, provider: str) -> ModelClient | None:
        client = self._clients.get(provider)
        if client is None:
            return None
        limit = self._concurrency_limits.get(provider)
        if not limit or limit <= 0:
            return client
        semaphore = self._semaphores.get(provider)
        if semaphore is None:
            semaphore = asyncio.Semaphore(limit)
            self._semaphores[provider] = semaphore
        return _ConcurrencyGuardedClient(client, semaphore)
