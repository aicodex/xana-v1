"""LLM driver abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from xana.llm_driver.contracts import ModelEvent, ModelRequest, ProviderCapabilities


class ModelClient(ABC):
    @abstractmethod
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...

    @abstractmethod
    async def cancel(self, request_id: str) -> None: ...

    @abstractmethod
    async def capabilities(self) -> ProviderCapabilities: ...


class ModelRegistry(ABC):
    @abstractmethod
    async def register(self, provider: str, client: ModelClient) -> None: ...

    @abstractmethod
    async def get(self, provider: str) -> ModelClient | None: ...
