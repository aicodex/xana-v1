"""Unified LLM HTTP/SSE/transport error classification and Retry-After parsing.

Centralizes error taxonomy so retry, stream recovery, compaction overflow,
credential refresh, and fallback policies all consume one authoritative classification.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import re
from collections.abc import Mapping
from typing import Any

import httpx

from xana.llm_driver.contracts import FailoverReason, ProviderError, ProviderErrorKind

_OVERFLOW_PATTERNS = (
    "context_length",
    "context window",
    "context length",
    "too many tokens",
    "prompt too long",
    "maximum context",
    "token limit",
    "context overflow",
    "exceeds the maximum",
)

_POLICY_REFUSAL_PATTERNS = (
    "content_policy",
    "safety",
    "guardrail",
    "policy_violation",
    "content_filter",
    "data_policy",
    "output blocked",
)

_QUOTA_PATTERNS = (
    "insufficient_quota",
    "quota exceeded",
    "quota_exhausted",
    "out of credits",
    "billing",
    "balance is insufficient",
)

_OVERLOADED_PATTERNS = (
    "overloaded",
    "capacity",
    "temporarily unavailable",
    "high demand",
)

_RETRY_IN_TEXT_RE = re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|s|sec|seconds)?", re.I)


def parse_retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """Parse Retry-After (numeric seconds or RFC 7231 HTTP-date) and retry-after-ms."""
    if not headers:
        return None
    lowered = {k.lower(): v for k, v in headers.items()}

    ms_val = lowered.get("retry-after-ms")
    if ms_val:
        try:
            secs = float(ms_val.strip()) / 1000.0
            if secs >= 0:
                return secs
        except ValueError:
            pass

    raw = lowered.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    try:
        secs = float(raw)
        return max(0.0, secs)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        delta = (parsed - dt.datetime.now(dt.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _extract_error_fields(body_text: str) -> tuple[str, str, dict[str, Any]]:
    """Extract (code, message, raw_dict) from provider JSON or plain text body."""
    raw: dict[str, Any] = {}
    code = ""
    message = body_text.strip()
    if body_text.strip().startswith("{"):
        try:
            parsed = json.loads(body_text)
            if isinstance(parsed, dict):
                raw = parsed
                err = parsed.get("error")
                if isinstance(err, dict):
                    code = str(err.get("code") or err.get("type") or "")
                    message = str(err.get("message") or message)
                elif isinstance(err, str):
                    message = err
                else:
                    code = str(parsed.get("code") or parsed.get("type") or "")
                    message = str(parsed.get("message") or message)
        except json.JSONDecodeError:
            pass
    return code, message, raw


def _parse_retry_from_message(text: str) -> float | None:
    match = _RETRY_IN_TEXT_RE.search(text)
    if not match:
        return None
    val = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    return val / 1000.0 if unit == "ms" else val


def classify_http_error(
    status: int,
    body_text: str,
    *,
    headers: Mapping[str, str] | None = None,
    request_id: str | None = None,
) -> ProviderError:
    """Classify an HTTP error status + response body into a structured ProviderError."""
    code, message, raw = _extract_error_fields(body_text)
    haystack = f"{code} {message} {body_text[:1000]}".lower()
    retry_after = parse_retry_after_seconds(headers)

    if status in (401, 403):
        return ProviderError(
            kind="auth",
            message=message or f"HTTP {status} authentication error",
            retryable=False,
            request_id=request_id,
            raw_status=status,
            raw=raw,
            reason="auth",
            should_rotate_credential=(status == 401),
            should_fallback=(status == 403),
        )

    if status == 402:
        return ProviderError(
            kind="rate_limit",
            message=message or "HTTP 402 quota/billing exhausted",
            retryable=False,
            request_id=request_id,
            raw_status=status,
            raw=raw,
            reason="quota_exhausted",
            should_rotate_credential=True,
            should_fallback=True,
        )

    if status == 413:
        return ProviderError(
            kind="bad_request",
            message=message or "HTTP 413 payload too large",
            retryable=False,
            request_id=request_id,
            raw_status=status,
            raw=raw,
            reason="payload_too_large",
            should_compress=True,
        )

    if status == 429:
        if any(p in haystack for p in _QUOTA_PATTERNS):
            reason: FailoverReason = "quota_exhausted"
            retryable = False
            should_rotate = True
        elif "upstream" in haystack:
            reason = "upstream_rate_limit"
            retryable = True
            should_rotate = False
        else:
            reason = "rate_limit"
            retryable = True
            should_rotate = False
        if retry_after is None:
            retry_after = _parse_retry_from_message(message)
        if retry_after is not None:
            raw = {**raw, "retry_after_s": retry_after}
        return ProviderError(
            kind="rate_limit",
            message=message or "HTTP 429 rate limited",
            retryable=retryable,
            request_id=request_id,
            raw_status=status,
            raw=raw,
            reason=reason,
            retry_after_s=retry_after,
            should_rotate_credential=should_rotate,
            should_fallback=True,
        )

    if status in (408, 499):
        return ProviderError(
            kind="timeout",
            message=message or f"HTTP {status} request timeout",
            retryable=True,
            request_id=request_id,
            raw_status=status,
            raw=raw,
            reason="timeout",
            retry_after_s=retry_after,
        )

    if status == 529 or (status >= 500 and any(p in haystack for p in _OVERLOADED_PATTERNS)):
        return ProviderError(
            kind="server_error",
            message=message or f"HTTP {status} provider overloaded",
            retryable=True,
            request_id=request_id,
            raw_status=status,
            raw=raw,
            reason="overloaded",
            retry_after_s=retry_after,
            should_fallback=True,
        )

    if status >= 500:
        return ProviderError(
            kind="server_error",
            message=message or f"HTTP {status} server error",
            retryable=True,
            request_id=request_id,
            raw_status=status,
            raw=raw,
            reason="server_error",
            retry_after_s=retry_after,
            should_fallback=True,
        )

    if 400 <= status < 500:
        if any(p in haystack for p in _OVERFLOW_PATTERNS):
            return ProviderError(
                kind="bad_request",
                message=message or f"HTTP {status} context length exceeded",
                retryable=False,
                request_id=request_id,
                raw_status=status,
                raw=raw,
                reason="context_overflow",
                should_compress=True,
            )
        if any(p in haystack for p in _POLICY_REFUSAL_PATTERNS):
            return ProviderError(
                kind="bad_request",
                message=message or f"HTTP {status} policy refusal",
                retryable=False,
                request_id=request_id,
                raw_status=status,
                raw=raw,
                reason="policy_refusal",
            )
        return ProviderError(
            kind="bad_request",
            message=message or f"HTTP {status} bad request",
            retryable=False,
            request_id=request_id,
            raw_status=status,
            raw=raw,
            reason="bad_request",
        )

    return ProviderError(
        kind="unknown",
        message=message or f"HTTP {status}",
        retryable=False,
        request_id=request_id,
        raw_status=status,
        raw=raw,
        reason="unknown",
    )


def classify_stream_eof(message: str = "stream ended before terminal event") -> ProviderError:
    """Explicit error for mid-stream EOF without a terminal done/error frame."""
    return ProviderError(
        kind="server_error",
        message=message,
        retryable=True,
        reason="stream_eof",
    )


def classify_exception(exc: BaseException, *, request_id: str | None = None) -> ProviderError:
    """Classify transport/network/timeout exceptions into structured ProviderError."""
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(
            kind="timeout",
            message=str(exc) or type(exc).__name__,
            retryable=True,
            request_id=request_id,
            reason="timeout",
        )
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.NetworkError)):
        return ProviderError(
            kind="server_error",
            message=str(exc) or type(exc).__name__,
            retryable=True,
            request_id=request_id,
            reason="connection",
            should_fallback=True,
        )
    return ProviderError(
        kind="unknown",
        message=str(exc) or type(exc).__name__,
        retryable=False,
        request_id=request_id,
        reason="unknown",
    )


def classify_sse_error_event(error_obj: dict[str, Any], *, request_id: str | None = None) -> ProviderError:
    """Classify an in-stream JSON error frame (Anthropic/OpenAI SSE error event)."""
    err_type = str(error_obj.get("type") or error_obj.get("code") or "")
    message = str(error_obj.get("message") or json.dumps(error_obj, ensure_ascii=False))
    status = error_obj.get("status") or error_obj.get("status_code")
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    if isinstance(status, int) and not isinstance(status, bool) and status >= 400:
        return classify_http_error(status, json.dumps({"error": error_obj}), request_id=request_id)
    haystack = f"{err_type} {message}".lower()
    if any(p in haystack for p in ("authentication_error", "permission_error", "invalid_api_key", "unauthorized")):
        return ProviderError(
            kind="auth", message=message, retryable=False, request_id=request_id,
            raw=dict(error_obj), reason="auth", should_rotate_credential=True,
        )
    if any(p in haystack for p in _QUOTA_PATTERNS):
        return ProviderError(
            kind="rate_limit", message=message, retryable=False, request_id=request_id,
            raw=dict(error_obj), reason="quota_exhausted", should_fallback=True,
        )
    if any(p in haystack for p in _POLICY_REFUSAL_PATTERNS):
        return ProviderError(
            kind="bad_request", message=message, retryable=False, request_id=request_id,
            raw=dict(error_obj), reason="policy_refusal",
        )
    if "overloaded" in haystack:
        return ProviderError(
            kind="server_error", message=message, retryable=True, request_id=request_id,
            raw=dict(error_obj), reason="overloaded", should_fallback=True,
        )
    if "rate_limit" in haystack:
        return ProviderError(
            kind="rate_limit", message=message, retryable=True, request_id=request_id,
            raw=dict(error_obj), reason="rate_limit", should_fallback=True,
        )
    if any(p in haystack for p in _OVERFLOW_PATTERNS):
        return ProviderError(
            kind="bad_request", message=message, retryable=False, request_id=request_id,
            raw=dict(error_obj), reason="context_overflow", should_compress=True,
        )
    if "invalid_request" in haystack or "bad_request" in haystack:
        return ProviderError(
            kind="bad_request", message=message, retryable=False, request_id=request_id,
            raw=dict(error_obj), reason="bad_request",
        )
    return ProviderError(
        kind="server_error", message=message, retryable=True, request_id=request_id,
        raw=dict(error_obj), reason="server_error",
    )
