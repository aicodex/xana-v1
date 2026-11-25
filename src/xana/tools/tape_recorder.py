"""No-op-capable Tape recorder for tool execution and normalized observations."""
from __future__ import annotations

import uuid

from xana.protocol.primitives import ComponentVersion
from xana.protocol.tape import TapeWriter
from xana.tools.contracts import ToolInvocation, ToolObservation, ToolResult


class ToolTapeRecorder:
    """Writes `tool.started`, `tool.observation`, and `tool.finished` events to Stage 0 `TapeWriter`."""

    def __init__(self, writer: TapeWriter | None = None) -> None:
        self._writer = writer

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    def record_call(
        self,
        *,
        invocation: ToolInvocation,
        result: ToolResult,
        observation: ToolObservation,
        elapsed_ms: float,
        obs_policy: ComponentVersion | None = None,
    ) -> None:
        if self._writer is None:
            return
        span_id = f"tool:{invocation.execution_id or uuid.uuid4().hex[:12]}"
        self._writer.append(
            "tool.finished",
            actor=f"tool.{invocation.tool_name}",
            provenance="tool",
            trace_id=invocation.run_id or "",
            span_id=span_id,
            latency_ms=round(elapsed_ms, 2),
            payload={
                "call_id": invocation.call_id,
                "tool_name": invocation.tool_name,
                "ok": result.ok,
                "failure_kind": result.failure_kind,
                "truncated": observation.truncated,
                "omitted_chars": observation.omitted_chars,
                "taint": observation.taint,
                "model_text": observation.model_text,
                "obs_policy_version": obs_policy.policy_version if obs_policy else None,
            },
        )
