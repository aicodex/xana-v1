"""LLM driver frozen data contracts (schema_version = 1).

The driver calls providers, parses streaming/non-streaming responses, classifies errors,
and runs pluggable request policies. It does not execute tools or mutate memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from xana.multimodal.contracts import InputModality, MediaInput
from xana.protocol.primitives import ErrorCategory, ProtocolError, TokenUsage

LLM_DRIVER_SCHEMA_VERSION = 1

ModelEventKind = Literal[
    "text_delta",
    "tool_call",
    "usage",
    "retry",
    "done",
    "error",
    "rate_limited",
]

ProviderErrorKind = Literal[
    "rate_limit",
    "auth",
    "bad_request",
    "server_error",
    "timeout",
    "cancelled",
    "unknown",
]

FailoverReason = Literal[
    "rate_limit",
    "upstream_rate_limit",
    "overloaded",
    "quota_exhausted",
    "auth",
    "context_overflow",
    "payload_too_large",
    "bad_request",
    "policy_refusal",
    "server_error",
    "timeout",
    "connection",
    "stream_eof",
    "empty_response",
    "cancelled",
    "unknown",
]

WireDialect = Literal["openai_chat", "anthropic_messages"]
MaxTokensParam = Literal["max_tokens", "max_completion_tokens"]
ThinkingParamStyle = Literal["none", "kimi", "anthropic"]
RequestPriority = Literal["foreground", "background"]
OutcomeStatus = Literal["done", "failed", "aborted"]

_REASON_TO_CATEGORY: dict[str, ErrorCategory] = {
    "rate_limit": "rate_limit",
    "upstream_rate_limit": "rate_limit",
    "quota_exhausted": "rate_limit",
    "overloaded": "provider",
    "server_error": "provider",
    "connection": "provider",
    "stream_eof": "provider",
    "empty_response": "provider",
    "auth": "permission",
    "context_overflow": "validation",
    "payload_too_large": "validation",
    "bad_request": "validation",
    "policy_refusal": "permission",
    "timeout": "timeout",
    "cancelled": "cancelled",
    "unknown": "internal",
}


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
    input_modalities: tuple[InputModality, ...] = ()


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
    reason: FailoverReason = "unknown"
    retry_after_s: float | None = None
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False

    def to_protocol(self) -> ProtocolError:
        resolved_reason = self.reason if self.reason != "unknown" else _kind_to_default_reason(self.kind)
        category: ErrorCategory = _REASON_TO_CATEGORY.get(resolved_reason, "internal")
        details: dict[str, Any] = {}
        if self.raw_status is not None:
            details["raw_status"] = self.raw_status
        if self.request_id is not None:
            details["request_id"] = self.request_id
        if self.retry_after_s is not None:
            details["retry_after_s"] = self.retry_after_s
        if self.should_compress:
            details["should_compress"] = True
        if self.should_rotate_credential:
            details["should_rotate_credential"] = True
        if self.should_fallback:
            details["should_fallback"] = True
        if self.raw:
            details["raw"] = dict(self.raw)
        return ProtocolError(
            code=f"provider.{resolved_reason}",
            message=self.message,
            category=category,
            retryable=self.retryable,
            details=details,
        )


def _kind_to_default_reason(kind: ProviderErrorKind) -> FailoverReason:
    mapping: dict[ProviderErrorKind, FailoverReason] = {
        "rate_limit": "rate_limit",
        "auth": "auth",
        "bad_request": "bad_request",
        "server_error": "server_error",
        "timeout": "timeout",
        "cancelled": "cancelled",
        "unknown": "unknown",
    }
    return mapping.get(kind, "unknown")


@dataclass(frozen=True)
class ModelMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    media: tuple[MediaInput, ...] = ()
    provider_data: dict[str, Any] = field(default_factory=dict)


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
    max_tokens: int | None = None
    priority: RequestPriority = "foreground"
    trace_id: str | None = None
    turn_id: str | None = None
    parent_span_id: str | None = None


def _add_opt_int(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def _add_opt_float(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None

    def is_empty(self) -> bool:
        return (
            self.input_tokens is None
            and self.output_tokens is None
            and self.cache_read_tokens is None
            and self.cache_write_tokens is None
            and self.cost_usd is None
            and self.reasoning_tokens is None
            and self.total_tokens is None
        )

    def add(self, other: "ModelUsage") -> "ModelUsage":
        if self.is_empty():
            return other
        if other.is_empty():
            return self
        return ModelUsage(
            input_tokens=_add_opt_int(self.input_tokens, other.input_tokens),
            output_tokens=_add_opt_int(self.output_tokens, other.output_tokens),
            cache_read_tokens=_add_opt_int(self.cache_read_tokens, other.cache_read_tokens),
            cache_write_tokens=_add_opt_int(self.cache_write_tokens, other.cache_write_tokens),
            cost_usd=_add_opt_float(self.cost_usd, other.cost_usd),
            reasoning_tokens=_add_opt_int(self.reasoning_tokens, other.reasoning_tokens),
            total_tokens=_add_opt_int(self.total_tokens, other.total_tokens),
        )

    def to_protocol(self) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens or 0,
            output_tokens=self.output_tokens or 0,
            cache_read_tokens=self.cache_read_tokens or 0,
            cache_write_tokens=self.cache_write_tokens or 0,
            reasoning_tokens=self.reasoning_tokens or 0,
            total_tokens=self.total_tokens,
            cost_usd=self.cost_usd,
        )


@dataclass(frozen=True)
class ModelEvent:
    kind: ModelEventKind
    payload: dict[str, Any] = field(default_factory=dict)
    usage: ModelUsage | None = None
    rate_limit: RateLimitSignal | None = None
    error: ProviderError | None = None


@dataclass(frozen=True)
class ModelOutcome:
    """Settled summary of one logical LLM request (across all attempts)."""

    status: OutcomeStatus
    text: str = ""
    thinking: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    stop_reason: str = ""
    usage: ModelUsage = field(default_factory=ModelUsage)
    wasted_usage: ModelUsage = field(default_factory=ModelUsage)
    error: ProviderError | None = None
    attempts: int = 1
    ttft_ms: float | None = None
    latency_ms: float = 0.0
    wasted_retry_ms: float = 0.0
    request_hash: str = ""
    prefix_hash: str = ""
    response_hash: str = ""
    provider: str = ""
    model: str = ""
    provider_request_id: str | None = None


@dataclass(frozen=True)
class ProviderProfile:
    """Declarative vendor/endpoint profile paired with a WireTransport dialect."""

    name: str
    dialect: WireDialect
    base_url: str
    max_tokens_param: MaxTokensParam = "max_tokens"
    default_max_tokens: int = 8192
    default_context_window: int | None = None
    model_max_tokens: dict[str, int] = field(default_factory=dict)
    supports_prompt_cache_key: bool = False
    supports_cache_control: bool = False
    supports_parallel_tools: bool = True
    supports_thinking: bool = False
    preserve_thinking_history: bool = False
    thinking_param_style: ThinkingParamStyle = "none"
    supported_efforts: tuple[str, ...] = ()
    omit_temperature: bool = False
    enable_dsml_recovery: bool = False
    supports_native_web_search: bool = False
    default_headers: dict[str, str] = field(default_factory=dict)
    default_input_modalities: tuple[InputModality, ...] = ()
    model_input_modalities: dict[str, tuple[InputModality, ...]] = field(default_factory=dict)
    timeout_s: float = 120.0
    idle_timeout_s: float | None = None
    anthropic_version: str = "2023-06-01"
    trust_env: bool = True

    def resolved_max_tokens(self, model: str, request_max_tokens: int | None) -> int:
        if request_max_tokens is not None:
            return request_max_tokens
        return self.model_max_tokens.get(model, self.default_max_tokens)

    def resolved_modalities(self, model: str | None) -> tuple[InputModality, ...]:
        if model and model in self.model_input_modalities:
            return self.model_input_modalities[model]
        return self.default_input_modalities
