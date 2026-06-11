"""In-memory ActiveContextStore + HistoryMirror.

Default runtime authority for active conversation context and durable mirror.
Single-process, non-persistent — suitable for CLI sessions and tests.
"""
from __future__ import annotations

from dataclasses import replace

from xana.memory.contracts import ActiveContext, HistoryRow, InjectedBlock, MemoryMessage, ReplayResult
from xana.memory.interfaces import ActiveContextStore, HistoryMirror, MemoryInjector, MemoryInjectorRegistry


class InMemoryActiveContextStore(ActiveContextStore):
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], ActiveContext] = {}

    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None:
        return self._store.get((session_id, agent_name))

    async def set(self, context: ActiveContext) -> None:
        self._store[(context.session_id, context.agent_name)] = context

    async def append_message(self, session_id: str, agent_name: str, message: MemoryMessage) -> ActiveContext:
        key = (session_id, agent_name)
        current = self._store.get(key) or ActiveContext(session_id=session_id, agent_name=agent_name)
        updated = replace(
            current,
            messages=current.messages + (message,),
            token_estimate=current.token_estimate + len(message.content),
        )
        self._store[key] = updated
        return updated

    async def compact(self, session_id: str, agent_name: str, summary: str) -> ActiveContext:
        key = (session_id, agent_name)
        current = self._store.get(key) or ActiveContext(session_id=session_id, agent_name=agent_name)
        updated = replace(current, summary=summary, messages=())
        self._store[key] = updated
        return updated

    async def clear(self, session_id: str, agent_name: str) -> None:
        self._store.pop((session_id, agent_name), None)


class InMemoryHistoryMirror(HistoryMirror):
    def __init__(self) -> None:
        self._rows: list[HistoryRow] = []

    async def append(self, row: HistoryRow) -> None:
        self._rows.append(row)

    async def replay(self) -> ReplayResult:
        sessions = {row.session_id for row in self._rows}
        return ReplayResult(loaded_sessions=len(sessions), loaded_rows=len(self._rows))

    def rows(self) -> tuple[HistoryRow, ...]:
        return tuple(self._rows)


class DefaultMemoryInjectorRegistry(MemoryInjectorRegistry):
    """Ordered registry of MemoryInjectors. Lower priority runs first."""

    def __init__(self) -> None:
        self._items: list[tuple[int, MemoryInjector]] = []

    async def register(self, injector: MemoryInjector, priority: int = 100) -> None:
        self._items.append((priority, injector))
        self._items.sort(key=lambda x: x[0])

    async def list_active(self) -> tuple[MemoryInjector, ...]:
        return tuple(inj for _, inj in self._items)

    async def collect(
        self,
        session_id: str,
        agent_name: str,
        user_input: str,
        cwd: str,
    ) -> tuple[InjectedBlock, ...]:
        out: list[InjectedBlock] = []
        seen_paths: set[str] = set()
        for _, injector in self._items:
            blocks = await injector.select(session_id, agent_name, user_input, cwd)
            for block in blocks:
                if block.node_path in seen_paths:
                    continue
                seen_paths.add(block.node_path)
                out.append(block)
        return tuple(out)
