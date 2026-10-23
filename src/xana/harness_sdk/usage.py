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
        # context used = 最近一次模型调用的完整 prompt:cache 命中/写入也占上下文,
        # 必须并回 input(provider 把 cached 部分从 input_tokens 里拆走了)。
        prompt_parts = (u.input_tokens, u.cache_read_tokens, u.cache_write_tokens)
        prompt_total = sum(p or 0 for p in prompt_parts)
        has_prompt = any(p is not None for p in prompt_parts) and prompt_total > 0
        new_in = prompt_total if has_prompt else current.exact_input_tokens
        new_out = u.output_tokens if u.output_tokens is not None else current.exact_output_tokens
        return replace(
            current, exact_input_tokens=new_in, exact_output_tokens=new_out,
            total_input_tokens=current.total_input_tokens + prompt_total,
            total_output_tokens=current.total_output_tokens + (u.output_tokens or 0),
            total_cache_read_tokens=current.total_cache_read_tokens + (u.cache_read_tokens or 0),
            model_calls=current.model_calls + 1,
        )
