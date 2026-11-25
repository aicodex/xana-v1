"""项目记忆的注入:强制注入的索引与置顶记忆 + 每条消息按 SAG2 检索召回。

ProjectMemoryInjector 是唯一的 MemoryInjector:每条用户消息返回
- index 块:全部记忆的索引(harness 变化才重新注入);
- pinned 块:置顶记忆全文;
- recall 块:SAG2 检索选中的记忆全文(带"写于 N 天前"的过时提示)。
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Literal

from xana.memory.contracts import (
    InjectedBlock,
    MemoryRecord,
    RetrievalContext,
    RetrievalPlan,
    SearchQuery,
)
from xana.memory.interfaces import (
    MemoryInjector,
    MemoryRetrievalPolicy,
    MemorySearchEngine,
    MemoryStore,
)
from xana.memory.store import search_terms
from xana.memory.tape_recorder import MemoryTapeRecorder
from xana.protocol.diag import DiagLogger
from xana.protocol.primitives import ComponentVersion

_diag = DiagLogger("memory")

RecallMode = Literal["sag2", "off"]
RECALL_MAX = 5
RECALL_BODY_CHARS = 2000
RECALL_TOTAL_CHARS = 6000
RECALL_MIN_TERMS = 2          # 检索词太少(如"好的""继续")不召回;中文不以空格判断,用检索词数
INDEX_PATH = "memory/(索引)"
PINNED_PATH = "memory/(置顶)"


class DefaultMemoryRetrievalPolicy(MemoryRetrievalPolicy):
    """Default SAG2 retrieval + budget formatting policy (evolvable RSI seam)."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        search_engine: MemorySearchEngine | None = None,
        timeout_s: float = 30.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._search = search_engine
        self._timeout = timeout_s
        self._now = now

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="memory.retrieval.sag2",
            kind="memory_retrieval_policy",
            version="1.0.0",
            policy_version="sag2-v1",
        )

    async def plan(self, ctx: RetrievalContext) -> RetrievalPlan:
        t0 = time.monotonic()
        records = ctx.records or self._store.list()
        if not records:
            return RetrievalPlan(skipped_reason="no_records", latency_ms=0.0)
        blocks: list[InjectedBlock] = [
            InjectedBlock(
                node_path=INDEX_PATH,
                content=self._store.index_text(records),
                placement="index",
                reason="memory_index",
            )
        ]
        pinned = self._store.pinned_text(records)
        if pinned:
            blocks.append(
                InjectedBlock(
                    node_path=PINNED_PATH,
                    content=pinned,
                    placement="pinned",
                    reason="memory_pinned",
                )
            )
        candidates = tuple(r for r in records if not r.pinned)
        if ctx.mode == "off":
            return RetrievalPlan(
                blocks=tuple(blocks),
                skipped_reason="mode_off",
                latency_ms=(time.monotonic() - t0) * 1000.0,
            )
        if not candidates:
            return RetrievalPlan(
                blocks=tuple(blocks),
                skipped_reason="no_unpinned_candidates",
                latency_ms=(time.monotonic() - t0) * 1000.0,
            )
        if len(set(search_terms(ctx.user_input))) < ctx.min_terms:
            return RetrievalPlan(
                blocks=tuple(blocks),
                skipped_reason="below_min_terms",
                latency_ms=(time.monotonic() - t0) * 1000.0,
            )
        chosen, judge_models = await self._choose(ctx.user_input, candidates, max_recall=ctx.max_recall)
        remaining = ctx.total_char_limit
        recalled_paths: list[str] = []
        for r in chosen:
            if remaining <= 0:
                break
            body = r.body[: min(ctx.body_char_limit, remaining)]
            if len(body) < len(r.body):
                body += "\n…(已截断,全文用 Read 查看)"
            remaining -= len(body)
            recalled_paths.append(r.path)
            blocks.append(
                InjectedBlock(
                    node_path=r.path,
                    content=f"{self._header(r)}\n\n{body}",
                    placement="recall",
                    reason="memory_sag2",
                )
            )
        latency_ms = (time.monotonic() - t0) * 1000.0
        _diag.perf(
            "recall.plan",
            latency_ms=latency_ms,
            slow_ms=2000.0,
            session_id=ctx.session_id,
            candidates=len(candidates),
            recalled=len(recalled_paths),
        )
        return RetrievalPlan(
            blocks=tuple(blocks),
            recalled_paths=tuple(recalled_paths),
            latency_ms=latency_ms,
            judge_models=judge_models,
        )

    async def _choose(
        self,
        query: str,
        candidates: tuple[MemoryRecord, ...],
        *,
        max_recall: int = RECALL_MAX,
    ) -> tuple[tuple[MemoryRecord, ...], tuple[str, ...]]:
        if self._search is None:
            raise ValueError("sag2 recall requires a configured search engine")
        hits = await asyncio.wait_for(
            self._search.search(SearchQuery(text=query, top_k=max_recall)),
            self._timeout,
        )
        by_path = {record.path: record for record in candidates}
        seen: set[str] = set()
        selected: list[MemoryRecord] = []
        models: set[str] = set()
        for hit in hits:
            for model_name in hit.metadata.get("judge_models", ()):
                models.add(str(model_name))
            if hit.node_path in by_path and hit.node_path not in seen:
                seen.add(hit.node_path)
                selected.append(by_path[hit.node_path])
                if len(selected) >= max_recall:
                    break
        return tuple(selected), tuple(sorted(models))

    def _header(self, r: MemoryRecord) -> str:
        """照 Claude Code memoryHeader:超过 1 天的写明天数与"可能过时"。"""
        days = max(0, int((self._now() - r.mtime) // 86400))
        if days > 1:
            return (
                f"这条记忆写于 {days} 天前。记忆是当时的记录,不是实时状态;涉及具体文件、数字、状态的,"
                f"使用前先核实。\n\n记忆:{r.name}({r.path}):"
            )
        return f"记忆(写于{'今天' if days == 0 else '昨天'}):{r.name}({r.path}):"


class ProjectMemoryInjector(MemoryInjector):
    def __init__(
        self,
        store: MemoryStore,
        *,
        mode: RecallMode = "sag2",
        timeout_s: float = 30.0,
        search_engine: MemorySearchEngine | None = None,
        now: Callable[[], float] = time.time,
        policy: MemoryRetrievalPolicy | None = None,
        tape_recorder: MemoryTapeRecorder | None = None,
    ) -> None:
        """sag2 直接使用所注入检索实例的结果，不在评分失败时回退其他检索实现。"""
        self._store = store
        self._mode = mode
        self._search = search_engine
        self._policy: MemoryRetrievalPolicy = policy or DefaultMemoryRetrievalPolicy(
            store,
            search_engine=search_engine,
            timeout_s=timeout_s,
            now=now,
        )
        self._tape = tape_recorder or MemoryTapeRecorder()

    async def name(self) -> str:
        return "project_memory"

    async def select(
        self,
        session_id: str,
        agent_name: str,
        user_input: str,
        cwd: str,
    ) -> tuple[InjectedBlock, ...]:
        records = self._store.list()
        if not records:
            return ()
        ctx = RetrievalContext(
            session_id=session_id,
            agent_name=agent_name,
            user_input=user_input,
            cwd=cwd,
            records=records,
            mode=self._mode,
            max_recall=RECALL_MAX,
            body_char_limit=RECALL_BODY_CHARS,
            total_char_limit=RECALL_TOTAL_CHARS,
            min_terms=RECALL_MIN_TERMS,
        )
        plan = await self._policy.plan(ctx)
        self._tape.record_recall(ctx, plan, policy=self._policy.component_version())
        return plan.blocks
