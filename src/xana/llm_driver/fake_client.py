"""Fake LLM client for tests and offline development.

Scripts a sequence of ModelEvents: text deltas, optional tool call, usage, done.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

from xana.llm_driver.contracts import ModelEvent, ModelRequest, ModelUsage, ProviderCapabilities
from xana.llm_driver.interfaces import ModelClient, ModelRegistry


@dataclass(frozen=True)
class ScriptedTurn:
    text_deltas: tuple[str, ...] = ()
    tool_call: tuple[str, dict[str, Any], str] | None = None  # (tool_name, input, call_id)
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0


class FakeModelClient(ModelClient):
    def __init__(self, *turns: ScriptedTurn) -> None:
        self._turns: list[ScriptedTurn] = list(turns) or [ScriptedTurn()]
        self._cursor = 0

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        turn = self._turns[min(self._cursor, len(self._turns) - 1)]
        self._cursor += 1
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
