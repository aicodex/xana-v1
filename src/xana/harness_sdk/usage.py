"""DefaultUsageAccumulator: provider-event-driven token tracking.

Mirrors cc's updateUsage rule (services/api/claude.ts):
  - Take exact_input_tokens/exact_output_tokens from ModelUsage when present
    (None or 0 means "carry over the previous value").
  - estimated_input_tokens is set once via estimate_before_request() and
    carried forward; never overwritten by exact-value updates.

We trust the provider's usage events as source of truth; we do not run a
local tokenizer. If a provider reports nothing, the snapshot stays at the
pre-flight estimate.
"""
from __future__ import annotations

from dataclasses import replace

from xana.harness_sdk.contracts import HarnessContext, HarnessRequest
from xana.harness_sdk.interfaces import UsageAccumulator
from xana.llm_driver.contracts import ModelEvent, ModelMessage
from xana.state.contracts import UsageSnapshot


_CHARS_PER_TOKEN = 4  # rough English heuristic, cc uses same for its prompt-size status line


class DefaultUsageAccumulator(UsageAccumulator):
    async def estimate_before_request(
        self,
        request: HarnessRequest,
        context: HarnessContext,
        messages: tuple[ModelMessage, ...],
    ) -> UsageSnapshot:
        total_chars = sum(len(m.content or "") for m in messages)
        est = max(1, total_chars // _CHARS_PER_TOKEN)
        return UsageSnapshot(
            model=context.model,
            provider=context.config_snapshot.get("provider"),
            estimated_input_tokens=est,
        )

    async def apply_model_event(self, current: UsageSnapshot, event: ModelEvent) -> UsageSnapshot:
        if event.kind != "usage" or event.usage is None:
            return current
        u = event.usage
        new_in = u.input_tokens if (u.input_tokens is not None and u.input_tokens > 0) else current.exact_input_tokens
        new_out = u.output_tokens if u.output_tokens is not None else current.exact_output_tokens
        return replace(current, exact_input_tokens=new_in, exact_output_tokens=new_out)
