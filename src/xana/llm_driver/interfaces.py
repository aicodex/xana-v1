"""LLM driver abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from xana.llm_driver.contracts import ModelEvent, ModelRequest, ModelUsage, ProviderCapabilities, ProviderRequest, ProviderToolSchema


class ModelRequestSerializer(ABC):
    @abstractmethod
    async def serialize(self, request: ModelRequest) -> ProviderRequest: ...


class ModelResponseParser(ABC):
    @abstractmethod
    async def parse_event(self, raw_event: object) -> ModelEvent: ...


class ToolSchemaAdapter(ABC):
    @abstractmethod
    async def adapt(self, provider: str, tool_schema: tuple[ProviderToolSchema, ...]) -> tuple[ProviderToolSchema, ...]: ...


class UsageParser(ABC):
    @abstractmethod
    async def parse_usage(self, raw_response: object) -> ModelUsage: ...


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
