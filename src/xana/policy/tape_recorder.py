"""No-op-capable Tape recorder for policy decisions and execution-environment runs."""
from __future__ import annotations

import uuid

from xana.policy.contracts import AuthorizationRequest, PermissionResult, ProcessOutcome, ProcessSpec
from xana.protocol.primitives import ComponentVersion
from xana.protocol.tape import TapeWriter


class PolicyTapeRecorder:
    """Writes `policy.decision` and `env.exec` events to a Stage 0 `TapeWriter`."""

    def __init__(self, writer: TapeWriter | None = None) -> None:
        self._writer = writer

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    def record_decision(
        self,
        req: AuthorizationRequest,
        result: PermissionResult,
        *,
        effective_mode: str,
        plan_mode: bool,
        risk: str | None = None,
        classifier: ComponentVersion | None = None,
        latency_ms: float = 0.0,
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "policy.decision",
            actor="policy",
            provenance="internal",
            trace_id=req.run_id,
            span_id=f"pol:{uuid.uuid4().hex[:12]}",
            latency_ms=round(latency_ms, 2),
            payload={
                "session_id": req.session_id,
                "call_id": req.call_id,
                "tool_name": req.tool_name,
                "read_only": req.read_only,
                "effective_mode": effective_mode,
                "plan_mode": plan_mode,
                "decision": result.decision,
                "reason": result.reason,
                "risk": risk,
                "classifier_version": classifier.policy_version if classifier else None,
                "classifier_hash": classifier.config_hash if classifier else None,
            },
        )

    def record_exec(
        self,
        spec: ProcessSpec,
        outcome: ProcessOutcome,
        *,
        env_version: ComponentVersion,
        trace_id: str = "",
    ) -> None:
        if self._writer is None:
            return
        self._writer.append(
            "env.exec",
            actor="exec_env",
            provenance="tool",
            trace_id=trace_id,
            span_id=f"exec:{spec.exec_id or uuid.uuid4().hex[:12]}",
            latency_ms=round(outcome.duration_ms, 2),
            payload={
                "cwd": spec.cwd,
                "mode": "shell" if spec.shell is not None else "argv",
                "command_preview": (spec.shell or " ".join(spec.argv or ()))[:200],
                "timeout_s": spec.timeout_s,
                "exit_code": outcome.exit_code,
                "timed_out": outcome.timed_out,
                "cancelled": outcome.cancelled,
                "truncated": outcome.truncated,
                "stdout_chars": len(outcome.stdout),
                "stderr_chars": len(outcome.stderr),
                "env_version": env_version.version,
            },
        )
