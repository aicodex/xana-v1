"""项目记忆的注入:强制注入的索引与置顶记忆 + 每条消息按语义召回(docs/design/memory-recall-design.md §3.2–3.3)。

ProjectMemoryInjector 是唯一的 MemoryInjector:每条用户消息返回
- index 块:全部记忆的索引(harness 变化才重新注入);
- pinned 块:置顶记忆全文;
- recall 块:选中记忆的全文(带"写于 N 天前"的过时提示)。
召回照 Claude Code findRelevantMemories:由选择器(runtime 注入,通常调用快速模型)根据
"这句话 + 记忆的名称与描述"挑出确定有用的最多 5 条;选择器超时或出错时退回字面检索。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Literal

from xana.memory.contracts import InjectedBlock, MemoryRecord, MemorySelector
from xana.memory.interfaces import MemoryInjector
from xana.memory.markdown import search_terms
from xana.memory.store import ProjectMemoryStore

_logger = logging.getLogger(__name__)

RecallMode = Literal["select", "lexical", "off"]
RECALL_MAX = 5
RECALL_BODY_CHARS = 2000
RECALL_TOTAL_CHARS = 6000
RECALL_MIN_TERMS = 2          # 检索词太少(如"好的""继续")不召回;中文不以空格判断,用检索词数
INDEX_PATH = "memory/(索引)"
PINNED_PATH = "memory/(置顶)"


class ProjectMemoryInjector(MemoryInjector):
    def __init__(self, store: ProjectMemoryStore, *, selector: MemorySelector | None = None,
                 mode: RecallMode = "select", timeout_s: float = 3.0,
                 now: Callable[[], float] = time.time) -> None:
        """selector 为 None 或 mode="lexical" 时只用字面检索;mode="off" 只注入索引与置顶记忆。"""
        self._store = store
        self._selector = selector
        self._mode = mode
        self._timeout = timeout_s
        self._now = now

    async def name(self) -> str:
        return "project_memory"

    async def select(self, session_id: str, agent_name: str, user_input: str,
                     cwd: str) -> tuple[InjectedBlock, ...]:
        records = self._store.list()
        if not records:
            return ()
        blocks = [InjectedBlock(node_path=INDEX_PATH, content=self._store.index_text(records),
                                placement="index", reason="memory_index")]
        pinned = self._store.pinned_text(records)
        if pinned:
            blocks.append(InjectedBlock(node_path=PINNED_PATH, content=pinned, placement="pinned",
                                        reason="memory_pinned"))
        candidates = tuple(r for r in records if not r.pinned)
        if self._mode == "off" or not candidates or len(set(search_terms(user_input))) < RECALL_MIN_TERMS:
            return tuple(blocks)
        chosen, reason = await self._choose(user_input, candidates)
        remaining = RECALL_TOTAL_CHARS
        for r in chosen:
            if remaining <= 0:
                break
            body = r.body[:min(RECALL_BODY_CHARS, remaining)]
            if len(body) < len(r.body):
                body += "\n…(已截断,全文用 Read 查看)"
            remaining -= len(body)
            blocks.append(InjectedBlock(node_path=r.path, content=f"{self._header(r)}\n\n{body}",
                                        placement="recall", reason=reason))
        return tuple(blocks)

    async def _choose(self, query: str, candidates: tuple[MemoryRecord, ...]) -> tuple[tuple[MemoryRecord, ...], str]:
        if self._mode == "select" and self._selector is not None:
            try:
                slugs = await asyncio.wait_for(self._selector(query, candidates), self._timeout)
                by_slug = {r.slug: r for r in candidates}
                return tuple(by_slug[s] for s in dict.fromkeys(slugs) if s in by_slug)[:RECALL_MAX], "memory_select"
            except Exception as exc:   # 超时 / 模型出错:退回字面检索,不阻断对话
                _logger.warning("记忆召回选择失败,退回字面检索:%r", exc)
        return self._store.lexical_rank(query, candidates, RECALL_MAX), "memory_lexical"

    def _header(self, r: MemoryRecord) -> str:
        """照 Claude Code memoryHeader:超过 1 天的写明天数与"可能过时"。"""
        days = max(0, int((self._now() - r.mtime) // 86400))
        if days > 1:
            return (f"这条记忆写于 {days} 天前。记忆是当时的记录,不是实时状态;涉及具体文件、数字、状态的,"
                    f"使用前先核实。\n\n记忆:{r.name}({r.path}):")
        return f"记忆(写于{'今天' if days == 0 else '昨天'}):{r.name}({r.path}):"
