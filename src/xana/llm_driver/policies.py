"""Evolvable LLM request policies (v1 defaults), version-stamped for RSI and Tape.

Each policy implements its ABC from `xana.llm_driver.interfaces`, exposes
`component_version() -> ComponentVersion` with deterministic `config_hash`,
and can be swapped independently without touching transports or callers.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, replace
from typing import Callable

from xana.llm_driver.contracts import ModelRequest, ProviderError
from xana.llm_driver.interfaces import (
    RequestRewriteStrategy,
    RetryDecision,
    RetryPolicyStrategy,
    RouteTarget,
    RoutingStrategy,
    RewriteDecision,
    StreamRecoveryDecision,
    StreamRecoveryStrategy,
)
from xana.protocol.primitives import ComponentVersion
from xana.protocol.versioning import canonical_json, sha256_text

_OVERFLOW_NUMS_RE = re.compile(
    r"maximum context length is\s+(\d+)\s+tokens.*?(?:resulted in\s+(\d+)\s+tokens|(\d+)\s+in the messages)",
    re.I | re.S,
)


def _config_hash(data: dict) -> str:
    return sha256_text(canonical_json(data))[:16]


class DefaultRetryPolicy(RetryPolicyStrategy):
    """Exponential backoff + jitter, Retry-After adherence, background 529 fast-shed."""

    POLICY_VERSION = "1.0.0"

    def __init__(
        self,
        *,
        max_retries: int = 3,
        base_delay_s: float = 1.0,
        max_delay_s: float = 30.0,
        jitter_ratio: float = 0.1,
        shed_background_529: bool = True,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._base_delay_s = base_delay_s
        self._max_delay_s = max_delay_s
        self._jitter_ratio = jitter_ratio
        self._shed_background_529 = shed_background_529
        self._rng = rng or random.random

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="retry",
            kind="llm_policy",
            version=self.POLICY_VERSION,
            policy_version=self.POLICY_VERSION,
            config_hash=_config_hash({
                "max_retries": self._max_retries,
                "base_delay_s": self._base_delay_s,
                "max_delay_s": self._max_delay_s,
                "jitter_ratio": self._jitter_ratio,
                "shed_background_529": self._shed_background_529,
            }),
        )

    def decide(
        self,
        error: ProviderError,
        *,
        attempt: int,
        request: ModelRequest,
    ) -> RetryDecision:
        if error.reason in ("policy_refusal", "context_overflow", "payload_too_large", "bad_request", "cancelled"):
            return RetryDecision(should_retry=False, reason_code=f"terminal_{error.reason}")
        if not error.retryable:
            return RetryDecision(should_retry=False, reason_code="non_retryable")
        if self._shed_background_529 and request.priority == "background" and error.reason == "overloaded":
            return RetryDecision(should_retry=False, reason_code="background_529_shed")

        rp = request.retry_policy
        limit = rp.max_retries if rp is not None else self._max_retries
        if rp is not None:
            if error.kind == "rate_limit" and not rp.retry_on_rate_limit:
                return RetryDecision(should_retry=False, reason_code="rate_limit_retry_disabled")
            if error.kind == "server_error" and not rp.retry_on_server_error:
                return RetryDecision(should_retry=False, reason_code="server_error_retry_disabled")
        if attempt > limit:
            return RetryDecision(should_retry=False, reason_code="max_retries_exhausted")

        if error.retry_after_s is not None and error.retry_after_s >= 0:
            delay = min(error.retry_after_s, self._max_delay_s)
            return RetryDecision(should_retry=True, delay_s=delay, reason_code="retry_after")

        base = rp.base_delay_s if rp is not None else self._base_delay_s
        raw_delay = min(base * (2 ** max(0, attempt - 1)), self._max_delay_s)
        if self._jitter_ratio > 0:
            factor = 1.0 + (self._rng() * 2.0 - 1.0) * self._jitter_ratio
            raw_delay = max(0.0, min(self._max_delay_s, raw_delay * factor))
        return RetryDecision(should_retry=True, delay_s=raw_delay, reason_code=f"backoff_{error.reason}")


class DefaultStreamRecoveryPolicy(StreamRecoveryStrategy):
    """Mid-stream interruption policy: invisible-delta drops retry cleanly; visible drops retry up to cap."""

    POLICY_VERSION = "1.0.0"

    def __init__(self, *, max_stream_retries: int = 3) -> None:
        self._max_stream_retries = max_stream_retries

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="stream_recovery",
            kind="llm_policy",
            version=self.POLICY_VERSION,
            policy_version=self.POLICY_VERSION,
            config_hash=_config_hash({"max_stream_retries": self._max_stream_retries}),
        )

    def decide(
        self,
        error: ProviderError,
        *,
        attempt: int,
        has_visible_output: bool,
        has_tool_calls: bool,
        request: ModelRequest,
    ) -> StreamRecoveryDecision:
        if not error.retryable or error.reason in ("cancelled", "policy_refusal", "auth", "quota_exhausted"):
            return StreamRecoveryDecision(action="abort", reason_code="non_recoverable")
        if attempt > self._max_stream_retries:
            return StreamRecoveryDecision(action="abort", reason_code="stream_retries_exhausted")
        if not has_visible_output and not has_tool_calls:
            return StreamRecoveryDecision(action="retry", reason_code="invisible_delta_drop")
        return StreamRecoveryDecision(action="retry", reason_code="mid_stream_retry")


class DefaultRequestRewritePolicy(RequestRewriteStrategy):
    """Prefix-preserving request rewrite on context_overflow (downgrades max_tokens only)."""

    POLICY_VERSION = "1.0.0"

    def __init__(self, *, min_max_tokens: int = 1024, headroom_tokens: int = 1000) -> None:
        self._min_max_tokens = min_max_tokens
        self._headroom_tokens = headroom_tokens

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="request_rewrite",
            kind="llm_policy",
            version=self.POLICY_VERSION,
            policy_version=self.POLICY_VERSION,
            config_hash=_config_hash({
                "min_max_tokens": self._min_max_tokens,
                "headroom_tokens": self._headroom_tokens,
            }),
        )

    def rewrite_on_error(
        self,
        request: ModelRequest,
        error: ProviderError,
        *,
        attempt: int,
    ) -> RewriteDecision | None:
        if error.reason != "context_overflow" or attempt > 2:
            return None
        current = request.max_tokens
        if current is None or current <= self._min_max_tokens:
            return None

        match = _OVERFLOW_NUMS_RE.search(error.message)
        if match:
            limit_tok = int(match.group(1))
            input_tok = int(match.group(2) or match.group(3))
            available = limit_tok - input_tok - self._headroom_tokens
            if available < self._min_max_tokens:
                return None
            new_max = min(current - 1, available) if available < current else max(self._min_max_tokens, current // 2)
        else:
            new_max = max(self._min_max_tokens, current // 2)

        if new_max >= current:
            return None
        return RewriteDecision(
            request=replace(request, max_tokens=new_max),
            mutated_fields=("max_tokens",),
            reason_code="overflow_max_tokens_downgrade",
        )


class DirectRoutingPolicy(RoutingStrategy):
    """Single-target pass-through routing (default when no fallback chain is configured)."""

    POLICY_VERSION = "1.0.0"

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="routing",
            kind="llm_policy",
            version=self.POLICY_VERSION,
            policy_version=self.POLICY_VERSION,
            config_hash=_config_hash({"mode": "direct"}),
        )

    def select_target(
        self,
        request: ModelRequest,
        *,
        failed_targets: tuple[tuple[str, str], ...] = (),
        last_error: ProviderError | None = None,
    ) -> RouteTarget | None:
        target = (request.provider, request.model)
        if target in failed_targets:
            return None
        return RouteTarget(provider=request.provider, model=request.model, reason_code="direct")


class FallbackRoutingPolicy(RoutingStrategy):
    """Ordered failover chain across (provider, model) targets on fallback-eligible errors."""

    POLICY_VERSION = "1.0.0"

    def __init__(self, *, fallbacks: tuple[tuple[str, str], ...] = ()) -> None:
        self._fallbacks = tuple(fallbacks)

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="routing",
            kind="llm_policy",
            version=self.POLICY_VERSION,
            policy_version=self.POLICY_VERSION,
            config_hash=_config_hash({"mode": "fallback", "fallbacks": list(self._fallbacks)}),
        )

    def select_target(
        self,
        request: ModelRequest,
        *,
        failed_targets: tuple[tuple[str, str], ...] = (),
        last_error: ProviderError | None = None,
    ) -> RouteTarget | None:
        primary = (request.provider, request.model)
        if primary not in failed_targets:
            return RouteTarget(provider=primary[0], model=primary[1], reason_code="primary")
        if last_error is not None and (
            not last_error.should_fallback
            or last_error.reason in ("policy_refusal", "context_overflow", "bad_request", "cancelled")
        ):
            return None
        for prov, mod in self._fallbacks:
            if (prov, mod) not in failed_targets:
                return RouteTarget(provider=prov, model=mod, reason_code="fallback")
        return None


@dataclass(frozen=True)
class PolicyBundle:
    """Bundled evolvable policies injected into ModelDriver."""

    retry: RetryPolicyStrategy
    stream_recovery: StreamRecoveryStrategy
    rewrite: RequestRewriteStrategy
    routing: RoutingStrategy

    @classmethod
    def default(cls) -> "PolicyBundle":
        return cls(
            retry=DefaultRetryPolicy(),
            stream_recovery=DefaultStreamRecoveryPolicy(),
            rewrite=DefaultRequestRewritePolicy(),
            routing=DirectRoutingPolicy(),
        )

    def component_versions(self) -> tuple[ComponentVersion, ...]:
        return (
            self.retry.component_version(),
            self.stream_recovery.component_version(),
            self.rewrite.component_version(),
            self.routing.component_version(),
        )
