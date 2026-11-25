"""In-memory ModelRegistry with per-provider concurrency limiting."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import AsyncGenerator

from xana.llm_driver.contracts import ModelEvent, ModelRequest, ProviderCapabilities
from xana.llm_driver.interfaces import ModelClient, ModelRegistry


class _ConcurrencyGuardedClient(ModelClient):
    """Wraps a ModelClient with a per-provider asyncio.Semaphore."""

    def __init__(self, inner: ModelClient, limit: int) -> None:
        self._inner = inner
        self._sem = asyncio.Semaphore(limit)

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        async with self._sem:
            async with aclosing(self._inner.stream(request)) as inner_stream:
                async for event in inner_stream:
                    yield event

    async def cancel(self, request_id: str) -> None:
        await self._inner.cancel(request_id)

    async def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        return await self._inner.capabilities(model)


class InMemoryModelRegistry(ModelRegistry):
    """In-memory ModelRegistry with optional per-provider max_concurrent limits."""

    def __init__(self, *, concurrency_limits: dict[str, int] | None = None) -> None:
        self._clients: dict[str, ModelClient] = {}
        self._limits: dict[str, int] = dict(concurrency_limits or {})

    async def register(self, provider: str, client: ModelClient) -> None:
        limit = self._limits.get(provider)
        if limit is not None and limit > 0 and not isinstance(client, _ConcurrencyGuardedClient):
            client = _ConcurrencyGuardedClient(client, limit)
        self._clients[provider] = client

    async def get(self, provider: str) -> ModelClient | None:
        return self._clients.get(provider)
