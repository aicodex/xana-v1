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
from datetime import datetime
from pathlib import Path
from typing import Callable

from xana.config.paths import project_home
from xana.memory.contracts import ActiveContext, HistoryRow, InjectedBlock, MemoryMessage, Placeholder, ReplayResult
from xana.memory.interfaces import ActiveContextStore, HistoryMirror, MemoryInjector, MemoryInjectorRegistry

# TYPE_CHECKING-only import to avoid circular dependency at runtime.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from xana.context.sdk import ContextSDK


def _debug_on() -> bool:
    return os.environ.get("XANA_DEBUG_ACTCTX", "") in ("1", "true", "on", "yes")


def _debug_path() -> Path:
    override = os.environ.get("XANA_DEBUG_ACTCTX_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return project_home() / "logs" / "xana_debug_actctx.log"


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
    """ActiveContextStore over the ContextSDK buffer — the single authority for a session's
    context (docs/design/session-and-card-state.md §3). Reads convert the SDK buffer to
    MemoryMessage / Placeholder views; writes go into the buffer under the per-sid write lock."""

    def __init__(
        self,
        context_sdk: "ContextSDK",
        *,
        on_clear: Callable[[str, str], None] | None = None,
    ) -> None:
        self._sdk = context_sdk
        # Bug B fix: optional callback fired on clear(sid, agent) so harness
        # can pop stale per-(sid,agent) caches (e.g. _session_prompt_tokens).
        # See docs/design/session-and-card-state.md §1.5.
        self._on_clear = on_clear

    def sdk(self) -> "ContextSDK":
        return self._sdk

    def _ctx(self, session_id: str, agent_name: str) -> ActiveContext:
        from xana.memory._message_bridge import sdk_buffer_to_legacy_pure
        msgs = sdk_buffer_to_legacy_pure(self._sdk._store.buffer(session_id, agent_name))
        return ActiveContext(session_id=session_id, agent_name=agent_name, messages=msgs,
                             token_estimate=sum(len(m.content) for m in msgs))

    def _log(self, op: str, session_id: str, agent_name: str) -> None:
        if _debug_on():
            from xana.memory._message_bridge import sdk_buffer_to_legacy_mixed
            _log_state(op, session_id, agent_name,
                       sdk_buffer_to_legacy_mixed(self._sdk._store.buffer(session_id, agent_name)))

    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None:
        if not self._sdk._store.buffer(session_id, agent_name):
            self._log("get_miss", session_id, agent_name)
            return None
        self._log("get", session_id, agent_name)
        return self._ctx(session_id, agent_name)

    async def set(self, context: ActiveContext) -> None:
        from xana.memory._message_bridge import memory_message_to_sdk
        st = self._sdk._store.state(context.session_id)
        async with st.write_lock:
            buf = self._sdk._store.buffer(context.session_id, context.agent_name)
            buf[:] = [m for m in (memory_message_to_sdk(x) for x in context.messages) if m is not None]
        self._log("set", context.session_id, context.agent_name)

    async def append_message(self, session_id: str, agent_name: str, message: MemoryMessage) -> ActiveContext:
        from xana.memory._message_bridge import memory_message_to_sdk
        sdk_msg = memory_message_to_sdk(message)
        if sdk_msg is not None:
            async with self._sdk._store.state(session_id).write_lock:
                self._sdk._store.buffer(session_id, agent_name).append(sdk_msg)
        self._log("append_msg", session_id, agent_name)
        return self._ctx(session_id, agent_name)

    async def clear(self, session_id: str, agent_name: str) -> None:
        """Wipe the SDK buffer for (sid, agent).

        Bug A fix: before clearing, scan for any CompactPlaceholder and release its
        compact_lock — otherwise a /clear during an in-flight compact would leak the
        lock and block the next /compact forever.
        Bug B fix: fire the ``on_clear`` callback (wired by boot) so harness-side
        per-(sid, agent) caches (``_session_prompt_tokens``) are popped.
        See docs/design/session-and-card-state.md §1.5.
        """
        from xana.context._compact_debug import log_lock
        from xana.context.message import CompactPlaceholder
        from xana.context.sdk import ContextSDK
        st = self._sdk._store.state(session_id)
        async with st.write_lock:
            buf = self._sdk._store.buffer(session_id, agent_name)
            for item in list(buf):
                if isinstance(item, CompactPlaceholder):
                    log_lock("leak_release", session_id, item._internal.run_id, "store.clear")
                    ContextSDK._release_compact(st, item._internal.run_id)
            buf.clear()
        _log_state("clear", session_id, agent_name, ())
        if self._on_clear is not None:
            try:
                self._on_clear(session_id, agent_name)
            except Exception:
                pass

    async def snapshot(
        self, session_id: str, agent_name: str
    ) -> tuple[MemoryMessage | Placeholder, ...]:
        from xana.memory._message_bridge import sdk_buffer_to_legacy_mixed
        snap = sdk_buffer_to_legacy_mixed(self._sdk._store.buffer(session_id, agent_name))
        _log_state("snapshot", session_id, agent_name, snap)
        return snap

    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None:
        from xana.memory._message_bridge import placeholder_to_sdk
        async with self._sdk._store.state(session_id).write_lock:
            self._sdk._store.buffer(session_id, agent_name).append(placeholder_to_sdk(placeholder))
        self._log("append_ph", session_id, agent_name)

    async def replace_placeholder(
        self,
        run_id: str,
        replace_with: tuple[MemoryMessage, ...],
    ) -> ActiveContext | None:
        """Replace the placeholder ``run_id`` (in whichever buffer holds it) with ``replace_with``.

        Bug A fix: if the replaced item is a CompactPlaceholder, release the per-sid
        compact_lock + signal the compact_event so snapshot_for_fork waiters unblock and
        future /compact calls don't deadlock (e.g. harness _finalize_writeback enqueuing
        ReplaceItem for a run whose run_id equals the compact PH's run_id).
        See docs/design/session-and-card-state.md §1.5 c4.
        """
        from xana.context.message import CompactPlaceholder
        from xana.context.sdk import ContextSDK
        from xana.memory._message_bridge import memory_message_to_sdk
        sdk_msgs = [m for m in (memory_message_to_sdk(x) for x in replace_with) if m is not None]
        store = self._sdk._store
        for (sid, agent), buf in list(store._buffers.items()):
            if store.find_by_run_id(buf, run_id) < 0:
                continue
            st = store.state(sid)
            async with st.write_lock:
                idx = store.find_by_run_id(buf, run_id)
                if idx < 0:
                    return None
                existing = buf[idx]
                buf[idx:idx + 1] = sdk_msgs
                if isinstance(existing, CompactPlaceholder):
                    from xana.context._compact_debug import log_lock
                    log_lock("leak_release", sid, run_id, "store.replace_placeholder")
                    ContextSDK._release_compact(st, run_id)
            self._log(f"replace_ph(run={run_id})", sid, agent)
            return self._ctx(sid, agent)
        return None


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
