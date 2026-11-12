"""LLM driver abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncGenerator, AsyncIterator

from xana.llm_driver.contracts import ModelEvent, ModelRequest, ProviderCapabilities


class ModelClient(ABC):
    @abstractmethod
    def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]: ...

    @abstractmethod
    async def cancel(self, request_id: str) -> None: ...

    @abstractmethod
    async def capabilities(self, model: str | None = None) -> ProviderCapabilities: ...


class ModelRegistry(ABC):
    @abstractmethod
    async def register(self, provider: str, client: ModelClient) -> None: ...

    @abstractmethod
    async def get(self, provider: str) -> ModelClient | None: ...
