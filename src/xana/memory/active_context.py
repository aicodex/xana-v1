"""Active conversation storage and input-note contributors."""
from xana.memory.contracts import ActiveContext, HistoryRow, InjectedBlock, MemoryMessage, ReplayResult
from xana.memory.interfaces import ActiveContextStore, HistoryMirror, MemoryInjector, MemoryInjectorRegistry
from xana.context.sdk import ContextSDK
from xana.memory._message_bridge import history_compaction_prefix, memory_message_to_sdk, messages_to_memory

class InMemoryActiveContextStore(ActiveContextStore):
    def __init__(self, context_sdk: ContextSDK) -> None:
        self._sdk = context_sdk

    def sdk(self) -> ContextSDK:
        return self._sdk

    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None:
        messages = await self.snapshot(session_id, agent_name)
        return ActiveContext(session_id, agent_name, messages, token_estimate=sum(len(m.content) for m in messages)) if messages else None

    async def set(self, context: ActiveContext) -> None:
        await self._sdk.replace(context.session_id, context.agent_name,
            tuple(m for item in context.messages if (m := memory_message_to_sdk(item)) is not None))

    async def append_message(self, session_id: str, agent_name: str, message: MemoryMessage) -> ActiveContext:
        item = memory_message_to_sdk(message)
        if item is not None:
            await self._sdk.append(session_id, agent_name, item)
        return await self.get(session_id, agent_name) or ActiveContext(session_id, agent_name)

    async def clear(self, session_id: str, agent_name: str) -> None:
        await self._sdk.replace(session_id, agent_name, ())

    async def snapshot(self, session_id: str, agent_name: str) -> tuple[MemoryMessage, ...]:
        return messages_to_memory(await self._sdk.snapshot(session_id, agent_name))

class InMemoryHistoryMirror(HistoryMirror):
    def __init__(self) -> None:
        self._rows: list[HistoryRow] = []

    async def append(self, row: HistoryRow) -> bool:
        if not any(r.row_id == row.row_id for r in self._rows):
            self._rows.append(row)
            return True
        return False

    async def read_rows(self, session_id: str | None = None) -> tuple[HistoryRow, ...]:
        return tuple(r for r in self._rows if session_id is None or r.session_id == session_id)

    async def replay(self) -> ReplayResult:
        return ReplayResult(len({r.session_id for r in self._rows}), len(self._rows))

    async def compaction_prefix(self, session_id: str, agent_name: str, expected: tuple[MemoryMessage, ...]) -> tuple[str, ...]:
        return history_compaction_prefix(self._rows, session_id, agent_name, expected)

    async def compact(self, row_ids: tuple[str, ...], summary: HistoryRow) -> None:
        if any(r.row_id == summary.row_id for r in self._rows):
            return
        selected = set(row_ids)
        actual = tuple(r.row_id for r in self._rows if (r.session_id, r.agent_name) == (summary.session_id, summary.agent_name) and r.kind != "system")
        if actual[:len(row_ids)] != row_ids:
            raise ValueError("compaction prefix changed before commit")
        first = next((i for i,r in enumerate(self._rows) if r.row_id in selected), len(self._rows))
        self._rows = [r for r in self._rows if r.row_id not in selected]
        self._rows.insert(first, summary)

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
