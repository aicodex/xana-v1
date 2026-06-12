"""src/xana/extension_api/assembler.py

PromptAssembler: collect → sort by priority → slot → shared char budget.
base_parts (harness memory/skill blocks) are first-class and budget-exempt.
"""
from __future__ import annotations

from xana.extension_api.contracts import SLOT_ORDER, ExtensionData, PromptFragment
from xana.extension_api.registry import ExtensionRegistry


class PromptAssembler:
    def __init__(self, registry: ExtensionRegistry, budget_chars: int = 8000) -> None:
        self._registry = registry
        self._budget = budget_chars

    async def assemble(
        self,
        *,
        thread_store: ExtensionData,
        turn_store: ExtensionData,
        base_parts: list[str],
    ) -> str:
        fragments: list[PromptFragment] = []
        for contributor in self._registry.context:
            fragments.extend(
                await contributor.contribute_prompt(thread_store, turn_store)
            )
        fragments.sort(key=lambda f: f.priority)  # stable: ties keep add order

        by_slot: dict[str, list[str]] = {slot: [] for slot in SLOT_ORDER}
        remaining = self._budget
        for slot in SLOT_ORDER:
            for f in (x for x in fragments if x.slot == slot):
                if remaining <= 0:
                    break
                chunk = f.content[:remaining]
                remaining -= len(chunk)
                if chunk:
                    by_slot[slot].append(chunk)

        ordered = (
            by_slot["system_head"]
            + list(base_parts)
            + by_slot["system"]
            + by_slot["system_tail"]
        )
        return "\n\n".join(p for p in ordered if p)
