"""In-memory ActiveContextStore + HistoryMirror.

Default runtime authority for active conversation context and durable mirror.
Single-process, non-persistent — suitable for CLI sessions and tests.
"""
from __future__ import annotations

from dataclasses import replace

from xana.memory.contracts import ActiveContext, HistoryRow, InjectedBlock, MemoryMessage, Placeholder, ReplayResult
from xana.memory.interfaces import ActiveContextStore, HistoryMirror, MemoryInjector, MemoryInjectorRegistry


class InMemoryActiveContextStore(ActiveContextStore):
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], ActiveContext] = {}
        # per-(session,agent) 混合序列，按追加顺序记录 MemoryMessage 或 Placeholder。
        # 与 _store[key].messages 是独立视图：前者含 PH，后者只装 MemoryMessage。
        self._snapshots: dict[tuple[str, str], list[MemoryMessage | Placeholder]] = {}

    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None:
        return self._store.get((session_id, agent_name))

    async def set(self, context: ActiveContext) -> None:
        self._store[(context.session_id, context.agent_name)] = context
        self._snapshots[(context.session_id, context.agent_name)] = list(context.messages)

    async def append_message(self, session_id: str, agent_name: str, message: MemoryMessage) -> ActiveContext:
        key = (session_id, agent_name)
        current = self._store.get(key) or ActiveContext(session_id=session_id, agent_name=agent_name)
        updated = replace(
            current,
            messages=current.messages + (message,),
            token_estimate=current.token_estimate + len(message.content),
        )
        self._store[key] = updated
        self._snapshots.setdefault(key, []).append(message)
        return updated

    async def compact(self, session_id: str, agent_name: str, summary: str) -> ActiveContext:
        key = (session_id, agent_name)
        current = self._store.get(key) or ActiveContext(session_id=session_id, agent_name=agent_name)
        updated = replace(current, summary=summary, messages=())
        self._store[key] = updated
        self._snapshots[key] = []
        return updated

    async def clear(self, session_id: str, agent_name: str) -> None:
        self._store.pop((session_id, agent_name), None)
        self._snapshots.pop((session_id, agent_name), None)

    async def snapshot(
        self, session_id: str, agent_name: str
    ) -> tuple[MemoryMessage | Placeholder, ...]:
        return tuple(self._snapshots.get((session_id, agent_name), ()))

    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None:
        key = (session_id, agent_name)
        self._snapshots.setdefault(key, []).append(placeholder)
        if key not in self._store:
            self._store[key] = ActiveContext(session_id=session_id, agent_name=agent_name)

    async def replace_placeholder(
        self,
        run_id: str,
        replace_with: tuple[MemoryMessage, ...],
    ) -> ActiveContext | None:
        for key, items in self._snapshots.items():
            for idx, item in enumerate(items):
                if isinstance(item, Placeholder) and item.run_id == run_id:
                    items[idx:idx + 1] = list(replace_with)
                    msgs = tuple(x for x in items if isinstance(x, MemoryMessage))
                    current = self._store.get(key) or ActiveContext(
                        session_id=key[0], agent_name=key[1]
                    )
                    updated = replace(
                        current,
                        messages=msgs,
                        token_estimate=sum(len(m.content) for m in msgs),
                    )
                    self._store[key] = updated
                    return updated
        return None

    async def replace_range(
        self,
        session_id: str,
        agent_name: str,
        start_idx: int,
        end_idx: int,
        summary_message: MemoryMessage,
    ) -> ActiveContext | None:
        raise NotImplementedError("Group H")


class InMemoryHistoryMirror(HistoryMirror):
    def __init__(self) -> None:
        self._rows: list[HistoryRow] = []

    async def append(self, row: HistoryRow) -> None:
        self._rows.append(row)

    async def replay(self) -> ReplayResult:
        sessions = {row.session_id for row in self._rows}
        return ReplayResult(loaded_sessions=len(sessions), loaded_rows=len(self._rows))

    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None:
        raise NotImplementedError("Group B/C")

    async def replace_placeholder(
        self, run_id: str, messages: tuple[MemoryMessage, ...]
    ) -> None:
        raise NotImplementedError("Group B/C")

    async def replace_range(
        self,
        session_id: str,
        agent_name: str,
        start_idx: int,
        end_idx: int,
        summary_message: MemoryMessage,
    ) -> None:
        raise NotImplementedError("Group H")

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
