"""Default ObservationPolicy: central max_result_chars truncation, taint tagging, model/UI split."""
from __future__ import annotations

from xana.protocol.diag import DiagLogger
from xana.protocol.primitives import ComponentVersion
from xana.protocol.versioning import canonical_json, sha256_text
from xana.tools.contracts import ToolInvocation, ToolObservation, ToolResult, ToolSpec
from xana.tools.interfaces import ObservationPolicy

_DIAG = DiagLogger("tools")


def _truncate_head_tail(text: str, limit: int) -> tuple[str, int]:
    if limit <= 0 or len(text) <= limit:
        return text, 0
    half = max(1, limit // 2)
    omitted = len(text) - (2 * half)
    return f"{text[:half]}\n\n…[截断 {omitted} 字符]…\n\n{text[-half:]}", omitted


class DefaultObservationPolicy(ObservationPolicy):
    """Enforces ToolSpec.max_result_chars centrally, separates UI vs model text, and tags open_world taint."""

    POLICY_VERSION = "1.0.0"

    def __init__(self, *, fallback_max_chars: int = 50_000) -> None:
        self._fallback_max_chars = max(256, fallback_max_chars)

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="default_observation_policy",
            kind="tools_policy",
            version=self.POLICY_VERSION,
            policy_version=self.POLICY_VERSION,
            config_hash=sha256_text(canonical_json({"fallback_max_chars": self._fallback_max_chars}))[:16],
        )

    def normalize(
        self,
        invocation: ToolInvocation,
        spec: ToolSpec,
        result: ToolResult,
        *,
        rendered: str,
    ) -> ToolObservation:
        limit = spec.max_result_chars if spec.max_result_chars > 0 else self._fallback_max_chars
        model_text, omitted = _truncate_head_tail(rendered, limit)
        truncated = bool(result.truncated or omitted > 0)
        ui_text = str(result.metadata.get("display_text") or rendered)
        taint = "external_untrusted" if spec.open_world else None
        meta = dict(result.metadata)
        if taint is not None:
            meta.setdefault("taint", taint)
        if omitted > 0:
            meta["omitted_chars"] = omitted
            _DIAG.anomaly(
                "observation.truncated",
                tool=invocation.tool_name,
                orig_chars=len(rendered),
                limit=limit,
                omitted=omitted,
            )
        if not result.ok:
            _DIAG.anomaly(
                "tool.failed",
                tool=invocation.tool_name,
                failure_kind=result.failure_kind or "execution",
                error=(result.error or "")[:120],
            )
        return ToolObservation(
            model_text=model_text,
            ui_text=ui_text,
            truncated=truncated,
            omitted_chars=omitted,
            taint=taint,
            metadata=meta,
        )
