"""Fake LLM client for tests and offline development.

A `ScriptedTurn` defaults to empty. On a default turn `FakeModelClient.stream`
mimics the DeepSeek wire shape used by the real `provider_deepseek` client:

  1. multiple `text_delta` chunks — the assembled `ModelRequest` is echoed
     line-by-line so each chunk is its own SSE-style event;
  2. a single `tool_call` to `memory_search` derived from the last user
     message — exercises the harness tool-dispatch loop;
  3. more `text_delta` chunks for a post-tool wrap-up ending with the fixed
     `[fake-reply ok]` marker that proves the model actually replied;
  4. `usage` then `done`.

Pass explicit `text_deltas` / `tool_call` to a `ScriptedTurn` to override and
drive a precise sequence (no echo, no auto tool call).
"""
from __future__ import annotations

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
    text_deltas: tuple[str, ...] = ()
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
        if msg.role == "user" and msg.content.strip():
            return msg.content
    return "(no user input)"


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

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        turn = self._turns[min(self._cursor, len(self._turns) - 1)]
        self._cursor += 1
        is_default_turn = not turn.text_deltas and turn.tool_call is None
        if is_default_turn:
            async for event in self._default_turn_events(request, turn):
                yield event
            return

        for delta in turn.text_deltas:
            yield ModelEvent(kind="text_delta", payload={"text": delta})
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
        self, request: ModelRequest, turn: ScriptedTurn
    ) -> AsyncGenerator[ModelEvent, None]:
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

        wrapup = (
            f"[fake-reply] dispatched {_DEFAULT_TOOL_NAME}(query={query!r}); "
            f"continuing turn.\n{_FAKE_REPLY_MARKER}\n"
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

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="fake", supports_streaming=True, supports_tools=True)


class InMemoryModelRegistry(ModelRegistry):
    def __init__(self) -> None:
        self._clients: dict[str, ModelClient] = {}

    async def register(self, provider: str, client: ModelClient) -> None:
        self._clients[provider] = client

    async def get(self, provider: str) -> ModelClient | None:
        return self._clients.get(provider)
