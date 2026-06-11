"""LLM driver contracts.

The driver calls providers and parses responses. It does not run tools or write memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ModelEventKind = Literal["text_delta", "tool_call", "usage", "done", "error", "rate_limited"]
ProviderErrorKind = Literal["rate_limit", "auth", "bad_request", "server_error", "timeout", "cancelled", "unknown"]


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    supports_streaming: bool = True
    supports_tools: bool = True
    supports_parallel_tools: bool = False
    supports_cache_control: bool = False
    supports_json_schema: bool = False
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    retry_on_rate_limit: bool = True
    retry_on_server_error: bool = True
    base_delay_s: float = 1.0


@dataclass(frozen=True)
class RateLimitSignal:
    retry_after_s: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ProviderError:
    kind: ProviderErrorKind
    message: str
    retryable: bool
    request_id: str | None = None
    raw_status: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderRequest:
    provider: str
    request_id: str | None = None
    body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderToolSchema:
    provider: str
    schema: dict[str, Any]


@dataclass(frozen=True)
class ModelMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRequest:
    provider: str
    model: str
    messages: tuple[ModelMessage, ...]
    tools_schema: tuple[dict[str, Any], ...] = ()
    effort: str | int | None = None
    request_id: str | None = None
    retry_policy: RetryPolicy | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class ModelEvent:
    kind: ModelEventKind
    payload: dict[str, Any] = field(default_factory=dict)
    usage: ModelUsage | None = None
    rate_limit: RateLimitSignal | None = None
    error: ProviderError | None = None
