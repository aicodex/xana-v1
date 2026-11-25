"""No-op-capable Tape recorder and deterministic hashing for LLM Driver calls.

Writes directly to Stage 0 `xana.protocol.tape.TapeWriter` with zero parallel log format.
Large prompts/tool schemas/responses (>64 KiB) are serialized as canonical strings so
TapeWriter's CAS blob store deduplicates repeated prefixes into `blobs/<sha256>`.
"""
from __future__ import annotations

import uuid
from typing import Any

from xana.llm_driver.contracts import (
    ModelMessage,
    ModelOutcome,
    ModelRequest,
    ModelUsage,
    ProviderError,
)
from xana.protocol.primitives import ComponentVersion
from xana.protocol.tape import TapeWriter
from xana.protocol.versioning import canonical_json, sha256_text


def _wire_messages(messages: tuple[ModelMessage, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        row: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.metadata:
            row["metadata"] = m.metadata
        if m.media:
            row["media"] = [
                {"kind": item.kind, "mime_type": item.mime_type, "sha256": item.sha256, "size_bytes": item.size_bytes}
                for item in m.media
            ]
        out.append(row)
    return out


def compute_tools_schema_hash(tools_schema: tuple[dict[str, Any], ...]) -> str:
    return sha256_text(canonical_json(list(tools_schema)))[:16]


def compute_prefix_hash(request: ModelRequest) -> str:
    """SHA-256 of cache-sensitive prefix: system messages + tools_schema + prompt_cache_key."""
    system_msgs = [m.content for m in request.messages if m.role == "system"]
    payload = {
        "system": system_msgs,
        "tools_schema": list(request.tools_schema),
        "prompt_cache_key": request.metadata.get("prompt_cache_key"),
    }
    return sha256_text(canonical_json(payload))[:16]


def compute_request_hash(request: ModelRequest) -> str:
    """SHA-256 of canonical logical request (provider, model, messages, tools, effort, max_tokens)."""
    payload = {
        "provider": request.provider,
        "model": request.model,
        "messages": _wire_messages(request.messages),
        "tools_schema": list(request.tools_schema),
        "effort": request.effort,
        "max_tokens": request.max_tokens,
    }
    return sha256_text(canonical_json(payload))[:16]


def compute_response_hash(
    *,
    text: str,
    thinking: str,
    tool_calls: tuple[dict[str, Any], ...],
    stop_reason: str,
) -> str:
    payload = {
        "text": text,
        "thinking": thinking,
        "tool_calls": list(tool_calls),
        "stop_reason": stop_reason,
    }
    return sha256_text(canonical_json(payload))[:16]


class LlmTapeRecorder:
    """Records `llm.*` TapeEvents into an optional Stage 0 `TapeWriter`."""

    def __init__(self, writer: TapeWriter | None = None) -> None:
        self._writer = writer

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    def record_request(self, req: ModelRequest) -> str:
        call_span_id = f"llm_call:{uuid.uuid4().hex[:12]}"
        if self._writer is None:
            return call_span_id
        msgs_json = canonical_json(_wire_messages(req.messages))
        tools_json = canonical_json(list(req.tools_schema))
        self._writer.append(
            "llm.request",
            actor="llm_driver",
            provenance="internal",
            trace_id=req.trace_id or "",
            turn_id=req.turn_id,
            span_id=call_span_id,
            parent_span_id=req.parent_span_id,
            payload={
                "provider": req.provider,
                "model": req.model,
                "request_id": req.request_id,
                "priority": req.priority,
                "request_hash": compute_request_hash(req),
                "prefix_hash": compute_prefix_hash(req),
                "tools_schema_hash": compute_tools_schema_hash(req.tools_schema),
                "message_count": len(req.messages),
                "tool_count": len(req.tools_schema),
                "max_tokens": req.max_tokens,
                "effort": req.effort,
                "messages_json": msgs_json,
                "tools_schema_json": tools_json,
            },
        )
        return call_span_id

    def record_policy_decision(
        self,
        *,
        call_span_id: str,
        req: ModelRequest,
        policy: ComponentVersion,
        action: str,
        reason_code: str,
        delay_ms: float = 0.0,
        mutated_fields: tuple[str, ...] = (),
        from_target: tuple[str, str] | None = None,
        to_target: tuple[str, str] | None = None,
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "llm.policy.decision",
            actor="llm_driver",
            provenance="internal",
            trace_id=req.trace_id or "",
            turn_id=req.turn_id,
            span_id=f"llm_policy:{uuid.uuid4().hex[:12]}",
            parent_span_id=call_span_id,
            payload={
                "policy_kind": policy.name,
                "policy_version": policy.policy_version or policy.version,
                "config_hash": policy.config_hash,
                "action": action,
                "reason_code": reason_code,
                "delay_ms": round(delay_ms, 2),
                "mutated_fields": list(mutated_fields),
                "from_target": list(from_target) if from_target else None,
                "to_target": list(to_target) if to_target else None,
            },
        )

    def record_attempt_start(self, req: ModelRequest, *, attempt: int, call_span_id: str) -> str:
        attempt_span_id = f"llm_attempt:{uuid.uuid4().hex[:12]}"
        if self._writer is None:
            return attempt_span_id
        self._writer.append(
            "llm.attempt.start",
            actor=f"llm_driver.{req.provider}",
            provenance="internal",
            trace_id=req.trace_id or "",
            turn_id=req.turn_id,
            span_id=attempt_span_id,
            parent_span_id=call_span_id,
            payload={
                "attempt_index": attempt,
                "provider": req.provider,
                "model": req.model,
                "effective_request_hash": compute_request_hash(req),
                "prefix_hash": compute_prefix_hash(req),
            },
        )
        return attempt_span_id

    def record_attempt_first_byte(
        self,
        *,
        attempt: int,
        attempt_span_id: str,
        ttft_ms: float,
        http_status: int | None,
        provider_request_id: str | None,
        req: ModelRequest,
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "llm.attempt.first_byte",
            actor=f"llm_driver.{req.provider}",
            provenance="internal",
            trace_id=req.trace_id or "",
            turn_id=req.turn_id,
            span_id=attempt_span_id,
            latency_ms=round(ttft_ms, 2),
            payload={
                "attempt_index": attempt,
                "ttft_ms": round(ttft_ms, 2),
                "http_status": http_status,
                "provider_request_id": provider_request_id,
            },
        )

    def record_attempt_error(
        self,
        *,
        attempt: int,
        attempt_span_id: str,
        req: ModelRequest,
        error: ProviderError,
        elapsed_ms: float,
        partial_bytes: int = 0,
        partial_text_chars: int = 0,
        partial_tool_calls: int = 0,
        ttft_ms: float | None = None,
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "llm.attempt.error",
            actor=f"llm_driver.{req.provider}",
            provenance="internal",
            trace_id=req.trace_id or "",
            turn_id=req.turn_id,
            span_id=attempt_span_id,
            latency_ms=round(elapsed_ms, 2),
            lifecycle=True,
            payload={
                "attempt_index": attempt,
                "error": error.to_protocol(),
                "partial_bytes_received": partial_bytes,
                "partial_text_chars": partial_text_chars,
                "partial_tool_calls": partial_tool_calls,
                "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
                "elapsed_ms": round(elapsed_ms, 2),
            },
        )

    def record_attempt_end(
        self,
        *,
        attempt: int,
        attempt_span_id: str,
        req: ModelRequest,
        stop_reason: str,
        ttft_ms: float | None,
        stream_duration_ms: float,
        usage: ModelUsage,
        output_chars: int,
        tool_call_count: int,
        malformed_tool_calls: int,
        provider_request_id: str | None = None,
    ) -> None:
        if self._writer is None:
            return
        in_tok = usage.input_tokens or 0
        cache_read = usage.cache_read_tokens or 0
        denom = in_tok + cache_read
        cache_hit_ratio = round(cache_read / denom, 4) if denom > 0 else 0.0
        self._writer.append(
            "llm.attempt.end",
            actor=f"llm_driver.{req.provider}",
            provenance="assistant",
            trace_id=req.trace_id or "",
            turn_id=req.turn_id,
            span_id=attempt_span_id,
            latency_ms=round(stream_duration_ms, 2),
            usage=usage.to_protocol(),
            payload={
                "attempt_index": attempt,
                "stop_reason": stop_reason,
                "provider_request_id": provider_request_id,
                "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
                "stream_duration_ms": round(stream_duration_ms, 2),
                "output_chars": output_chars,
                "tool_call_count": tool_call_count,
                "malformed_tool_calls": malformed_tool_calls,
                "cache_hit_ratio": cache_hit_ratio,
            },
        )

    def record_response(
        self,
        *,
        call_span_id: str,
        req: ModelRequest,
        outcome: ModelOutcome,
    ) -> None:
        if self._writer is None:
            return
        content_json = canonical_json({
            "text": outcome.text,
            "thinking": outcome.thinking,
            "tool_calls": list(outcome.tool_calls),
        })
        self._writer.append(
            "llm.response",
            actor="llm_driver",
            provenance="assistant" if outcome.status == "done" else "internal",
            trace_id=req.trace_id or "",
            turn_id=req.turn_id,
            span_id=call_span_id,
            parent_span_id=req.parent_span_id,
            latency_ms=round(outcome.latency_ms, 2),
            usage=outcome.usage.to_protocol(),
            lifecycle=True,
            payload={
                "status": outcome.status,
                "request_hash": outcome.request_hash,
                "prefix_hash": outcome.prefix_hash,
                "response_hash": outcome.response_hash,
                "final_provider": outcome.provider,
                "final_model": outcome.model,
                "attempts_total": outcome.attempts,
                "stop_reason": outcome.stop_reason,
                "ttft_ms": round(outcome.ttft_ms, 2) if outcome.ttft_ms is not None else None,
                "wasted_retry_ms": round(outcome.wasted_retry_ms, 2),
                "wasted_usage": outcome.wasted_usage.to_protocol(),
                "error": outcome.error.to_protocol() if outcome.error is not None else None,
                "content_json": content_json,
            },
        )
