"""Memory abstract interfaces.

Two substrates, in order of distance from the conversation:
- ActiveContextStore + HistoryMirror: runtime conversation memory.
- MemorySearchEngine + MemoryInjector(+Registry): search-once / consume-twice.
  L1 (Injector) is invoked by PromptAssembler at turn start.
  L2 standard tool (memory_search) is a plain Tool calling the same MemorySearchEngine.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from xana.memory.contracts import (
    ActiveContext,
    ExtractionOutcome,
    ExtractionRequest,
    HistoryRow,
    InjectedBlock,
    MemoryMessage,
    MemoryNode,
    MemoryRecord,
    MemorySnapshot,
    ReplayResult,
    RetrievalContext,
    RetrievalPlan,
    SearchHit,
    SearchQuery,
)
from xana.protocol.primitives import ComponentVersion

if TYPE_CHECKING:
    from xana.context.sdk import ContextSDK


class ActiveContextStore(ABC):
    """会话上下文读写接口;权威数据在 ContextSDK 缓冲里(docs/design/session-and-card-state.md §3)。"""

    @abstractmethod
    def sdk(self) -> "ContextSDK":
        """背后的 ContextSDK:auto-compact / 手动 /compact 经它拿 per-sid compact 锁(§1.5 c3/c4)。"""

    @abstractmethod
    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None: ...

    @abstractmethod
    async def set(self, context: ActiveContext) -> None: ...

    @abstractmethod
    async def append_message(self, session_id: str, agent_name: str, message: MemoryMessage) -> ActiveContext: ...

    @abstractmethod
    async def clear(self, session_id: str, agent_name: str) -> None: ...

    @abstractmethod
    async def snapshot(
        self, session_id: str, agent_name: str
    ) -> tuple[MemoryMessage, ...]: ...



class HistoryMirror(ABC):
    @abstractmethod
    async def read_rows(self, session_id: str | None = None) -> tuple[HistoryRow, ...]: ...

    @abstractmethod
    async def append(self, row: HistoryRow) -> bool: ...


    @abstractmethod
    async def replay(self) -> ReplayResult: ...



    @abstractmethod
    async def compaction_prefix(self, session_id: str, agent_name: str,
                                expected: tuple[MemoryMessage, ...]) -> tuple[str, ...]:
        """Freeze row identities matching exactly the SDK compaction input."""

    @abstractmethod
    async def compact(self, row_ids: tuple[str, ...], summary: HistoryRow) -> None:
        """Replace only the captured prefix; preserve concurrent tail writes."""

class MemoryReplayer(ABC):
    @abstractmethod
    async def rebuild_active_context(self, mirror: HistoryMirror, store: ActiveContextStore) -> ReplayResult: ...


class MemorySearchEngine(ABC):
    """Pluggable retrieval backend (BM25 / embedding / hybrid / external).

    Caller passes raw text in SearchQuery; backend internally tokenizes or
    embeds. Score must be normalized higher-is-better.

    The same engine instance is consumed by both layers — L1 Injector and
    L2 query Tool — so callers must not assume single-tenancy.
    """

    @abstractmethod
    async def index(self, node: MemoryNode) -> None: ...

    @abstractmethod
    async def remove(self, node_path: str) -> None: ...

    @abstractmethod
    async def search(self, query: SearchQuery) -> tuple[SearchHit, ...]: ...


class MemoryInjector(ABC):
    """L1: turns request + context into prompt blocks.

    Implementations may or may not call MemorySearchEngine. AlwaysInjector
    does not search; KeywordInjector / PathMatchInjector do. The interface
    is uniform regardless.
    """

    @abstractmethod
    async def name(self) -> str: ...

    @abstractmethod
    async def select(
        self,
        session_id: str,
        agent_name: str,
        user_input: str,
        cwd: str,
    ) -> tuple[InjectedBlock, ...]: ...


class MemoryInjectorRegistry(ABC):
    """Multiple Injectors compose. PromptAssembler iterates in priority order.

    Lower priority int = runs earlier (its blocks appear first when placement
    matches). De-duplication of node_path across Injectors is the registry's
    responsibility.
    """

    @abstractmethod
    async def register(self, injector: MemoryInjector, priority: int = 100) -> None: ...

    @abstractmethod
    async def list_active(self) -> tuple[MemoryInjector, ...]: ...

    @abstractmethod
    async def collect(
        self,
        session_id: str,
        agent_name: str,
        user_input: str,
        cwd: str,
    ) -> tuple[InjectedBlock, ...]: ...


class MemoryStore(ABC):
    """Authoritative project memory store contract with RSI snapshot/fork/rollback."""

    @abstractmethod
    def list(self) -> tuple[MemoryRecord, ...]: ...

    @abstractmethod
    def get(self, slug: str) -> MemoryRecord | None: ...

    @abstractmethod
    def index_text(self, records: tuple[MemoryRecord, ...] | None = None) -> str: ...

    @abstractmethod
    def pinned_text(self, records: tuple[MemoryRecord, ...] | None = None) -> str: ...

    @abstractmethod
    def similar(
        self, name: str, description: str, *, exclude: str = ""
    ) -> tuple[MemoryRecord, float] | None: ...

    @abstractmethod
    def write(
        self,
        slug: str,
        *,
        name: str,
        description: str,
        mtype: str,
        body: str,
        pinned: bool = False,
        topic: str = "",
    ) -> MemoryRecord: ...

    @abstractmethod
    def append(self, slug: str, text: str) -> MemoryRecord: ...

    @abstractmethod
    def delete(self, slug: str) -> bool: ...

    @abstractmethod
    def snapshot(self) -> MemorySnapshot: ...

    @abstractmethod
    def fork(self, target_root: Path) -> "MemoryStore": ...

    @abstractmethod
    def rollback(self, snapshot: MemorySnapshot) -> None: ...


class MemoryRetrievalPolicy(ABC):
    """Evolvable retrieval & injection policy (SAG2 baseline; swappable for RSI)."""

    @abstractmethod
    def component_version(self) -> ComponentVersion: ...

    @abstractmethod
    async def plan(self, ctx: RetrievalContext) -> RetrievalPlan: ...


class MemoryExtractionPolicy(ABC):
    """Evolvable post-turn background memory extraction policy."""

    @abstractmethod
    def component_version(self) -> ComponentVersion: ...

    @abstractmethod
    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome: ...
