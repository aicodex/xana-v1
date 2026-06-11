"""Fake LLM client for tests and offline development.

A `ScriptedTurn` defaults to empty. On empty, `FakeModelClient.stream` echoes
the entire assembled `ModelRequest` (system + history) back as one text delta,
followed by a fixed `[fake-reply ok]` marker — so any caller can see exactly
what the harness produced: memory injection, skill blocks, full history. Pass
explicit `text_deltas` / `tool_call` to override and drive a precise sequence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncGenerator, AsyncIterator

from xana.llm_driver.contracts import ModelEvent, ModelMessage, ModelRequest, ModelUsage, ProviderCapabilities
from xana.llm_driver.interfaces import ModelClient, ModelRegistry


_FAKE_REPLY_MARKER = "[fake-reply ok]"


@dataclass(frozen=True)
class ScriptedTurn:
    text_deltas: tuple[str, ...] = ()
    tool_call: tuple[str, dict[str, Any], str] | None = None  # (tool_name, input, call_id)
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0


def _echo_prompt(messages: tuple[ModelMessage, ...]) -> str:
    parts = ["[fake-reply] echoed prompt:"]
    for msg in messages:
        parts.append(f"--- {msg.role} ---")
        parts.append(msg.content)
    parts.append("--- /prompt ---")
    parts.append(_FAKE_REPLY_MARKER)
    return "\n".join(parts)


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
            yield ModelEvent(kind="text_delta", payload={"text": _echo_prompt(request.messages)})
        else:
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
