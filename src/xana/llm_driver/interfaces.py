"""LLM driver abstract interfaces (frozen skeleton + evolvable policy seams)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from xana.llm_driver.contracts import (
    ModelEvent,
    ModelRequest,
    ProviderCapabilities,
    ProviderError,
    ProviderProfile,
)
from xana.protocol.primitives import ComponentVersion


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    delay_s: float = 0.0
    reason_code: str = ""


@dataclass(frozen=True)
class StreamRecoveryDecision:
    action: str  # "retry" | "abort"
    reason_code: str = ""


@dataclass(frozen=True)
class RewriteDecision:
    request: ModelRequest
    mutated_fields: tuple[str, ...] = ()
    reason_code: str = ""


@dataclass(frozen=True)
class RouteTarget:
    provider: str
    model: str
    reason_code: str = "direct"
    metadata: dict[str, Any] = field(default_factory=dict)


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


class CredentialResolver(ABC):
    """Decouples API key / OAuth token resolution from wire transports."""

    @property
    @abstractmethod
    def can_refresh(self) -> bool: ...

    @abstractmethod
    async def auth_headers(self, *, force_refresh: bool = False) -> dict[str, str]: ...


class WireTransport(ABC):
    """Stateless single-attempt wire dialect adapter (no retries, no silent fallback)."""

    @property
    @abstractmethod
    def dialect(self) -> str: ...

    @abstractmethod
    def stream_attempt(
        self,
        request: ModelRequest,
        profile: ProviderProfile,
        credentials: CredentialResolver,
    ) -> AsyncGenerator[ModelEvent, None]: ...

    @abstractmethod
    def capabilities(self, profile: ProviderProfile, model: str | None = None) -> ProviderCapabilities: ...

    @abstractmethod
    async def aclose(self) -> None: ...


class RetryPolicyStrategy(ABC):
    """Evolvable retry/backoff decision policy (stamped into Tape)."""

    @abstractmethod
    def component_version(self) -> ComponentVersion: ...

    @abstractmethod
    def decide(
        self,
        error: ProviderError,
        *,
        attempt: int,
        request: ModelRequest,
    ) -> RetryDecision: ...


class StreamRecoveryStrategy(ABC):
    """Evolvable mid-stream interruption recovery policy."""

    @abstractmethod
    def component_version(self) -> ComponentVersion: ...

    @abstractmethod
    def decide(
        self,
        error: ProviderError,
        *,
        attempt: int,
        has_visible_output: bool,
        has_tool_calls: bool,
        request: ModelRequest,
    ) -> StreamRecoveryDecision: ...


class RequestRewriteStrategy(ABC):
    """Evolvable pre-flight / error-driven request rewrite policy (prefix-preserving)."""

    @abstractmethod
    def component_version(self) -> ComponentVersion: ...

    @abstractmethod
    def rewrite_on_error(
        self,
        request: ModelRequest,
        error: ProviderError,
        *,
        attempt: int,
    ) -> RewriteDecision | None: ...


class RoutingStrategy(ABC):
    """Evolvable provider/model routing and fallback policy."""

    @abstractmethod
    def component_version(self) -> ComponentVersion: ...

    @abstractmethod
    def select_target(
        self,
        request: ModelRequest,
        *,
        failed_targets: tuple[tuple[str, str], ...] = (),
        last_error: ProviderError | None = None,
    ) -> RouteTarget | None: ...
