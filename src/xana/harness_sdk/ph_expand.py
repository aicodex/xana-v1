"""其他并发 run 的 Placeholder 在提示词里的展开(docs/design/session-and-card-state.md §1.3)。

每轮拼提示词时,混合快照里可能有其他并发 run 的 PH(它们的回答还没好):
- 普通 PH → 1 条 assistant 消息(用户原话已在它前面,不再重复);
- compact PH → 0 条(完全隐身)。
文本的唯一定义在 ``xana.context.message.regular_placeholder_text``,与 SDK 的 Message 序列化共用。
本 run 自己的 PH 永远不出现(fork 快照取于追加 PH 之前)。
"""
from __future__ import annotations

from typing import Iterable

from xana.context.message import regular_placeholder_text
from xana.memory.contracts import MemoryMessage, Placeholder


def expand_placeholders(msgs: Iterable[MemoryMessage | Placeholder]) -> tuple[MemoryMessage, ...]:
    items = tuple(msgs)
    if not any(isinstance(m, Placeholder) for m in items):
        return items  # type: ignore[return-value]
    out: list[MemoryMessage] = []
    for m in items:
        if not isinstance(m, Placeholder):
            out.append(m)
        elif not m.metadata.get("compact_placeholder"):
            out.append(MemoryMessage(role="assistant", content=regular_placeholder_text(m.label)))
    return tuple(out)
