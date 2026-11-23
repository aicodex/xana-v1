"""Minimal shared value primitives for Xana's protocol foundation.

Strictly Python stdlib-only. Frozen, JSON-wire-serializable dataclasses only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ErrorCategory = Literal[
    "validation",
    "permission",
    "sandbox",
    "timeout",
    "rate_limit",
    "provider",
    "execution",
    "cancelled",
    "not_found",
    "unsupported",
    "internal",
]

CaptureStatus = Literal["complete", "truncated", "omitted"]

Provenance = Literal[
    "system",
    "user",
    "assistant",
    "tool_trusted",
    "tool_untrusted",
    "evaluator",
    "internal",
]

ContentPartKind = Literal["text", "thinking", "media", "blob_ref"]


@dataclass(frozen=True)
class ProtocolError:
    code: str
    message: str
    category: ErrorCategory = "internal"
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int | None = None
    cost_usd: float | None = None

    def is_zero(self) -> bool:
        return (
            self.input_tokens == 0
            and self.output_tokens == 0
            and self.cache_read_tokens == 0
            and self.cache_write_tokens == 0
            and self.reasoning_tokens == 0
            and self.total_tokens is None
            and self.cost_usd is None
        )

    def add(self, other: "TokenUsage") -> "TokenUsage":
        if self.is_zero():
            return other
        if other.is_zero():
            return self
        if self.total_tokens is None and other.total_tokens is None:
            total: int | None = None
        else:
            left_total = (
                self.total_tokens
                if self.total_tokens is not None
                else self.input_tokens + self.output_tokens
            )
            right_total = (
                other.total_tokens
                if other.total_tokens is not None
                else other.input_tokens + other.output_tokens
            )
            total = left_total + right_total
        cost = (
            self.cost_usd + other.cost_usd
            if self.cost_usd is not None and other.cost_usd is not None
            else None
        )
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_tokens=total,
            cost_usd=cost,
        )


@dataclass(frozen=True)
class BlobRef:
    blob_id: str
    sha256: str
    media_type: str
    original_bytes: int
    retained_bytes: int
    capture: CaptureStatus = "complete"


@dataclass(frozen=True)
class ComponentVersion:
    name: str
    kind: str
    version: str
    config_hash: str = ""
    policy_version: str = ""


@dataclass(frozen=True)
class ContentPart:
    kind: ContentPartKind
    text: str = ""
    media_type: str = ""
    blob: BlobRef | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
