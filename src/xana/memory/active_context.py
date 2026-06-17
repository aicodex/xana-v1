"""In-memory ActiveContextStore + HistoryMirror.

Default runtime authority for active conversation context and durable mirror.
Single-process, non-persistent — suitable for CLI sessions and tests.

Debug switch:
    XANA_DEBUG_ACTCTX=1 (env var) → every mutation + get() writes the FULL
    main-context snapshot to a dedicated log file (default
    ~/.xana/logs/xana_debug_actctx.log; override with XANA_DEBUG_ACTCTX_FILE).
    Pure observation, never alters prompt content. Off by default (zero
    overhead beyond the env var lookup on each call).
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from xana.memory.contracts import ActiveContext, HistoryRow, InjectedBlock, MemoryMessage, Placeholder, ReplayResult
from xana.memory.interfaces import ActiveContextStore, HistoryMirror, MemoryInjector, MemoryInjectorRegistry


def _debug_on() -> bool:
    return os.environ.get("XANA_DEBUG_ACTCTX", "") in ("1", "true", "on", "yes")


def _debug_path() -> Path:
    override = os.environ.get("XANA_DEBUG_ACTCTX_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".xana" / "logs" / "xana_debug_actctx.log"


def _fmt_item(m: object) -> str:
    if isinstance(m, Placeholder):
        return f"PH(run={m.run_id} label={m.label!r:.40})"
    if isinstance(m, MemoryMessage):
        body = (m.content or "")[:80].replace("\n", "⏎")
        return f"{m.role}:{body!r}"
    return repr(m)


# READ vs WRITE classification — read ops never mutate state.
# Tag in the log helps tell "1 write + 1 follow-up read" apart from a real
# double write (which would have two WRITE entries for the same data).
_READ_OPS = {"get", "get_miss", "snapshot"}


def _log_state(
    op: str, session_id: str, agent_name: str,
    items: tuple | list,
) -> None:
    if not _debug_on():
        return
    path = _debug_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    ts = datetime.now().isoformat(timespec="milliseconds")
    kind = "READ " if op in _READ_OPS or op.split("(", 1)[0] in _READ_OPS else "WRITE"
    lines = [
        f"[ACTCTX] {ts} {kind} op={op} sid={session_id} agent={agent_name} n={len(items)}",
    ]
    for i, m in enumerate(items):
        lines.append(f"[ACTCTX] {ts}        [{i}] {_fmt_item(m)}")
    lines.append("")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except OSError:
        pass


class InMemoryActiveContextStore(ActiveContextStore):
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], ActiveContext] = {}
        # per-(session,agent) 混合序列，按追加顺序记录 MemoryMessage 或 Placeholder。
        # 与 _store[key].messages 是独立视图：前者含 PH，后者只装 MemoryMessage。
        self._snapshots: dict[tuple[str, str], list[MemoryMessage | Placeholder]] = {}

    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None:
        ctx = self._store.get((session_id, agent_name))
        if ctx is not None:
            _log_state("get", session_id, agent_name, ctx.messages)
        else:
            _log_state("get_miss", session_id, agent_name, ())
        return ctx

    async def set(self, context: ActiveContext) -> None:
        self._store[(context.session_id, context.agent_name)] = context
        self._snapshots[(context.session_id, context.agent_name)] = list(context.messages)
        _log_state("set", context.session_id, context.agent_name, context.messages)

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
        _log_state("append_msg", session_id, agent_name, updated.messages)
        return updated

    async def compact(self, session_id: str, agent_name: str, summary: str) -> ActiveContext:
        key = (session_id, agent_name)
        current = self._store.get(key) or ActiveContext(session_id=session_id, agent_name=agent_name)
        updated = replace(current, summary=summary, messages=())
        self._store[key] = updated
        self._snapshots[key] = []
        _log_state("compact", session_id, agent_name, ())
        return updated

    async def clear(self, session_id: str, agent_name: str) -> None:
        self._store.pop((session_id, agent_name), None)
        self._snapshots.pop((session_id, agent_name), None)
        _log_state("clear", session_id, agent_name, ())

    async def snapshot(
        self, session_id: str, agent_name: str
    ) -> tuple[MemoryMessage | Placeholder, ...]:
        snap = tuple(self._snapshots.get((session_id, agent_name), ()))
        _log_state("snapshot", session_id, agent_name, snap)
        return snap

    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None:
        key = (session_id, agent_name)
        self._snapshots.setdefault(key, []).append(placeholder)
        if key not in self._store:
            self._store[key] = ActiveContext(session_id=session_id, agent_name=agent_name)
        _log_state("append_ph", session_id, agent_name, self._snapshots[key])

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
                    _log_state(
                        f"replace_ph(run={run_id})", key[0], key[1], items,
                    )
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
        key = (session_id, agent_name)
        items = self._snapshots.get(key)
        if items is None:
            return None
        if start_idx < 0 or end_idx > len(items) or start_idx >= end_idx:
            return None
        items[start_idx:end_idx] = [summary_message]
        msgs = tuple(x for x in items if isinstance(x, MemoryMessage))
        current = self._store.get(key) or ActiveContext(
            session_id=session_id, agent_name=agent_name
        )
        updated = replace(
            current,
            messages=msgs,
            token_estimate=sum(len(m.content) for m in msgs),
        )
        self._store[key] = updated
        _log_state(
            f"replace_range[{start_idx}:{end_idx}]",
            session_id, agent_name, items,
        )
        return updated


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
