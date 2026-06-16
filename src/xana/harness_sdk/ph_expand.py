"""Placeholder → synthetic user/assistant pair expansion for LLM prompt.

When the per-run prompt assembler receives a mixed sequence of MemoryMessage
and Placeholder (representing OTHER concurrent runs whose answers aren't ready
yet), each Placeholder becomes:

    user:      {ph.label}
    assistant: [此问题正在后台处理中,请基于其他可用上下文继续。]

The run never sees its OWN Placeholder (the fork snapshot is taken BEFORE the
PH is appended to main). MemoryMessage entries pass through unchanged.
"""
from __future__ import annotations

from typing import Iterable

from xana.memory.contracts import MemoryMessage, Placeholder

PH_ASSISTANT_REPLY = "[此问题正在后台处理中,请基于其他可用上下文继续。]"


def expand_placeholders(
    msgs: Iterable[MemoryMessage | Placeholder],
) -> tuple[MemoryMessage, ...]:
    """Replace Placeholder nodes with synthetic user/assistant pairs.

    Fast-path: if the input has no Placeholder, returns the original tuple
    (or a tuple constructed from the iterable) untouched.
    """
    items = tuple(msgs)
    if not any(isinstance(m, Placeholder) for m in items):
        return items  # type: ignore[return-value]
    out: list[MemoryMessage] = []
    for m in items:
        if isinstance(m, Placeholder):
            out.append(MemoryMessage(role="user", content=m.label))
            out.append(MemoryMessage(role="assistant", content=PH_ASSISTANT_REPLY))
        else:
            out.append(m)
    return tuple(out)
