"""Memory abstract interfaces.

Three substrates, in order of distance from the conversation:
- ActiveContextStore + HistoryMirror: runtime conversation memory.
- MemoryTree: persistent knowledge tree (treemem). Atomic ops M1.4 live here.
- MemorySearchEngine + MemoryInjector(+Registry): search-once / consume-twice.
  L1 (Injector) is invoked by PromptAssembler at turn start.
  L2 standard tools (memory_search / memory_get) are plain Tools that call
  MemorySearchEngine + MemoryTree.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from xana.memory.contracts import (
    ActiveContext,
    HistoryRow,
    InjectedBlock,
    MemoryLintResult,
    MemoryMessage,
    MemoryNode,
    ReplayResult,
    SearchHit,
    SearchQuery,
)


class ActiveContextStore(ABC):
    @abstractmethod
    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None: ...

    @abstractmethod
    async def set(self, context: ActiveContext) -> None: ...

    @abstractmethod
    async def append_message(self, session_id: str, agent_name: str, message: MemoryMessage) -> ActiveContext: ...

    @abstractmethod
    async def compact(self, session_id: str, agent_name: str, summary: str) -> ActiveContext: ...

    @abstractmethod
    async def clear(self, session_id: str, agent_name: str) -> None: ...


class HistoryMirror(ABC):
    @abstractmethod
    async def append(self, row: HistoryRow) -> None: ...

    @abstractmethod
    async def replay(self) -> ReplayResult: ...


class MemoryReplayer(ABC):
    @abstractmethod
    async def rebuild_active_context(self, mirror: HistoryMirror, store: ActiveContextStore) -> ReplayResult: ...


class MemoryPolicy(ABC):
    @abstractmethod
    async def should_compact(self, context: ActiveContext) -> bool: ...

    @abstractmethod
    async def select_messages(self, context: ActiveContext) -> tuple[MemoryMessage, ...]: ...


class MemoryTree(ABC):
    """Storage + atomic operations for the knowledge tree (yuxu treemem).

    Read methods are cheap. Mutation methods are atomic and must succeed
    fully or leave the tree unchanged. Lint is read-only and may be called
    independently or during write paths.
    """

    @abstractmethod
    async def get_node(self, path: str) -> MemoryNode | None: ...

    @abstractmethod
    async def list_children(self, parent_path: str) -> tuple[MemoryNode, ...]: ...

    @abstractmethod
    async def lint(self, root_path: str = "") -> MemoryLintResult: ...

    # ---- M1.4 atomic ops ---------------------------------------------------

    @abstractmethod
    async def split_node(
        self,
        path: str,
        children: tuple[MemoryNode, ...],
        index_body: str,
    ) -> MemoryNode: ...

    @abstractmethod
    async def merge_nodes(
        self,
        survivor_path: str,
        absorbed_path: str,
        merged_body: str,
    ) -> MemoryNode: ...

    @abstractmethod
    async def move_node(self, path: str, new_parent_path: str) -> MemoryNode: ...

    @abstractmethod
    async def redefine_parent(self, parent_path: str, new_index_body: str) -> MemoryNode: ...

    @abstractmethod
    async def delete_node(self, path: str, reason: str) -> None: ...

    @abstractmethod
    async def rename_node(self, path: str, new_name: str) -> MemoryNode: ...


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
