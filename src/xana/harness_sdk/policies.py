"""Default versioned Harness policies (CompactionPolicy, StopDecisionPolicy).

These are the swappable RSI strategy seams extracted from DefaultHarness while
preserving exact baseline behavior.
"""
from __future__ import annotations

from xana.harness_sdk.context_notes import (
    CONTINUE_THINKING_ONLY,
    CONTINUE_TRUNCATED,
    NO_PROGRESS_REMINDER,
    stop_hook_text,
)
from xana.harness_sdk.contracts import (
    CompactionContext,
    CompactionDecision,
    StopContext,
    StopDecision,
)
from xana.harness_sdk.goal import GOAL_CONTINUATION
from xana.harness_sdk.interfaces import CompactionPolicy, StopDecisionPolicy
from xana.protocol.primitives import ComponentVersion


class DefaultCompactionPolicy(CompactionPolicy):
    """Default token-threshold auto-compaction & blocking policy."""

    def __init__(
        self,
        *,
        overflow_ratios: tuple[float, ...] = (0.7, 0.5, 0.35),
        max_retries: int = 3,
    ) -> None:
        self._overflow_ratios = overflow_ratios
        self._max_retries = max_retries

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="harness.compaction.default",
            kind="compaction_policy",
            version="1.0.0",
            policy_version="threshold-v1",
        )

    def evaluate(self, ctx: CompactionContext) -> CompactionDecision:
        if not ctx.context_window or ctx.force_compact is None or ctx.history_len <= 0:
            return CompactionDecision(
                action="none",
                effective_tokens=ctx.estimated_tokens,
                reason="disabled_or_empty",
                overflow_ratios=self._overflow_ratios,
                max_retries=self._max_retries,
            )
        tokens = ctx.estimated_tokens
        if ctx.exact_input_tokens is not None and ctx.baseline_tokens is not None:
            tokens = max(1, ctx.exact_input_tokens + ctx.estimated_tokens - ctx.baseline_tokens)
        if tokens >= int(ctx.force_compact) and not ctx.compaction_blocked:
            return CompactionDecision(
                action="compact",
                effective_tokens=tokens,
                reason="force_compact_reached",
                overflow_ratios=self._overflow_ratios,
                max_retries=self._max_retries,
            )
        if ctx.blocking_limit is not None and tokens >= int(ctx.blocking_limit):
            return CompactionDecision(
                action="block",
                effective_tokens=tokens,
                reason="blocking_limit_reached",
                overflow_ratios=self._overflow_ratios,
                max_retries=self._max_retries,
            )
        return CompactionDecision(
            action="none",
            effective_tokens=tokens,
            reason="within_budget",
            overflow_ratios=self._overflow_ratios,
            max_retries=self._max_retries,
        )


class DefaultStopDecisionPolicy(StopDecisionPolicy):
    """Default turn-stop and continuation cascade (Claude Code 2.1.281 semantics)."""

    def __init__(
        self,
        *,
        max_truncation_continues: int = 3,
        max_stop_hook_blocks: int = 8,
        silent_calls_before_reminder: int = 5,
        max_silent_reminders: int = 3,
    ) -> None:
        self._max_truncations = max_truncation_continues
        self._max_hook_blocks = max_stop_hook_blocks
        self._silent_calls_threshold = silent_calls_before_reminder
        self._max_silent_reminders = max_silent_reminders

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="harness.stop.default",
            kind="stop_decision_policy",
            version="1.0.0",
            policy_version="cascade-v1",
        )

    def decide_stop(self, ctx: StopContext) -> StopDecision:
        if not ctx.turn_text.strip() and ctx.had_thinking and not ctx.thinking_nudged:
            return StopDecision(
                action="continue",
                note_kind="continuation",
                note_text=CONTINUE_THINKING_ONLY,
                mark_thinking_nudged=True,
                reason="thinking_only",
            )
        if ctx.stop_reason == "length" and ctx.truncations < self._max_truncations:
            return StopDecision(
                action="continue",
                note_kind="continuation",
                note_text=CONTINUE_TRUNCATED,
                increment_truncations=True,
                reason="length_truncated",
            )
        if ctx.hook_block_reason is not None:
            if ctx.hook_blocks < self._max_hook_blocks:
                return StopDecision(
                    action="continue",
                    note_kind="stop_hook",
                    note_text=stop_hook_text(ctx.hook_block_reason),
                    increment_hook_blocks=True,
                    reason="stop_hook_blocked",
                )
            return StopDecision(
                action="stop",
                pause_goal_reason="stop_hook_or_verification_limit" if ctx.goal_active else None,
                reason="stop_hook_limit_reached",
            )
        if ctx.verify_failed_text is not None:
            if ctx.hook_blocks < self._max_hook_blocks:
                return StopDecision(
                    action="continue",
                    note_kind="verify",
                    note_text=ctx.verify_failed_text,
                    increment_hook_blocks=True,
                    reason="verify_failed",
                )
            return StopDecision(
                action="stop",
                pause_goal_reason="stop_hook_or_verification_limit" if ctx.goal_active else None,
                reason="verify_limit_reached",
            )
        if ctx.goal_active:
            if ctx.goal_completion_requested:
                return StopDecision(
                    action="stop",
                    complete_goal=True,
                    reason="goal_completed",
                )
            return StopDecision(
                action="continue",
                note_kind="goal",
                note_text=GOAL_CONTINUATION,
                reason="goal_continue",
            )
        return StopDecision(action="stop", reason="normal_stop")

    def decide_silent_reminder(
        self,
        *,
        silent_calls: int,
        silent_reminders: int,
        is_child_run: bool,
    ) -> str | None:
        if (
            not is_child_run
            and silent_calls >= self._silent_calls_threshold
            and silent_reminders < self._max_silent_reminders
        ):
            return NO_PROGRESS_REMINDER
        return None
