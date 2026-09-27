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
    def __init__(
        self,
        context_sdk: "ContextSDK | None" = None,
        *,
        on_clear: Callable[[str, str], None] | None = None,
    ) -> None:
        self._store: dict[tuple[str, str], ActiveContext] = {}
        # per-(session,agent) 混合序列，按追加顺序记录 MemoryMessage 或 Placeholder。
        # 与 _store[key].messages 是独立视图：前者含 PH，后者只装 MemoryMessage。
        self._snapshots: dict[tuple[str, str], list[MemoryMessage | Placeholder]] = {}
        # P3-D: when set, this store mirror-writes every mutation to the SDK
        # and serves reads from the SDK buffer (canonical). The legacy
        # in-memory buffers above are kept in sync for debug logging and as
        # a fallback when the SDK does not represent a payload (system
        # messages, currently).
        self._sdk: "ContextSDK | None" = context_sdk
        # Bug B fix: optional callback fired on clear(sid, agent) so harness
        # can pop stale per-(sid,agent) caches (e.g. _session_prompt_tokens).
        # See docs/design/session-and-card-state.md §1.5.
        self._on_clear = on_clear

    def sdk(self) -> "ContextSDK | None":
        """Return the wrapped ContextSDK (or None for legacy/test stores).

        Bug C fix: lets the harness drive auto-compact through the SDK
        (append_compact_placeholder + replace_placeholder) so it takes the
        same per-sid compact_lock as manual /compact and the system-role
        compact_boundary marker is no longer dropped by ``set()``'s mirror.
        See docs/design/session-and-card-state.md §1.5 c3/c4.
        """
        return self._sdk

    # ------------------------------------------------------------------
    # SDK bridge helpers
    # ------------------------------------------------------------------

    def _sdk_buffer(self, session_id: str, agent_name: str):
        if self._sdk is None:
            return None
        # Direct buffer access — same view ContextSDK uses internally; we
        # never mutate it from here.
        return self._sdk._store.buffer(session_id, agent_name)

    async def _sdk_mirror_append_message(
        self, session_id: str, agent_name: str, message: MemoryMessage
    ) -> None:
        if self._sdk is None:
            return
        from xana.memory._message_bridge import memory_message_to_sdk
        sdk_msg = memory_message_to_sdk(message)
        if sdk_msg is None:
            return
        st = self._sdk._store.state(session_id)
        async with st.write_lock:
            self._sdk._store.buffer(session_id, agent_name).append(sdk_msg)

    async def _sdk_mirror_append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None:
        if self._sdk is None:
            return
        from xana.memory._message_bridge import placeholder_to_sdk
        sdk_ph = placeholder_to_sdk(placeholder)
        st = self._sdk._store.state(session_id)
        async with st.write_lock:
            self._sdk._store.buffer(session_id, agent_name).append(sdk_ph)

    async def _sdk_mirror_replace_placeholder(
        self, run_id: str, replace_with: tuple[MemoryMessage, ...]
    ) -> None:
        """Mirror a legacy replace_placeholder into the SDK buffer.

        Bug A fix: if the SDK item being replaced is a CompactPlaceholder,
        release the per-sid compact_lock + signal the compact_event so
        snapshot_for_fork waiters unblock and future /compact calls don't
        deadlock. Without this the legacy writeback path (e.g. harness
        _finalize_writeback enqueuing ReplaceItem for a cancelled real run
        whose run_id happens to equal the compact PH's run_id) silently
        leaks the lock. See docs/design/session-and-card-state.md §1.5 c4.
        """
        if self._sdk is None:
            return
        from xana.context.message import CompactPlaceholder
        from xana.context.sdk import ContextSDK
        from xana.memory._message_bridge import memory_message_to_sdk
        sdk_msgs = [m for m in (memory_message_to_sdk(x) for x in replace_with) if m is not None]
        # Find the buffer that holds this run_id.
        for (sid, agent), buf in self._sdk._store._buffers.items():
            idx = self._sdk._store.find_by_run_id(buf, run_id)
            if idx < 0:
                continue
            st = self._sdk._store.state(sid)
            async with st.write_lock:
                existing = buf[idx]
                buf[idx:idx + 1] = sdk_msgs
                if isinstance(existing, CompactPlaceholder):
                    from xana.context._compact_debug import log_lock
                    log_lock("leak_release", sid, run_id,
                             "sdk_mirror_replace_placeholder")
                    ContextSDK._release_compact(st, run_id)
            return

    async def _sdk_mirror_replace_range(
        self,
        session_id: str,
        agent_name: str,
        start_idx: int,
        end_idx: int,
        summary_message: MemoryMessage,
    ) -> None:
        if self._sdk is None:
            return
        from xana.memory._message_bridge import memory_message_to_sdk
        sdk_msg = memory_message_to_sdk(summary_message)
        st = self._sdk._store.state(session_id)
        async with st.write_lock:
            buf = self._sdk._store.buffer(session_id, agent_name)
            if start_idx < 0 or end_idx > len(buf) or start_idx >= end_idx:
                return
            buf[start_idx:end_idx] = [sdk_msg] if sdk_msg is not None else []

    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None:
        if self._sdk is not None:
            from xana.memory._message_bridge import sdk_buffer_to_legacy_pure
            buf = self._sdk_buffer(session_id, agent_name)
            if buf is None or not buf:
                # No SDK data yet — fall back to legacy view (e.g. before
                # any writes happen, or for system-only contexts).
                ctx = self._store.get((session_id, agent_name))
                _log_state("get" if ctx else "get_miss",
                           session_id, agent_name, ctx.messages if ctx else ())
                return ctx
            msgs = sdk_buffer_to_legacy_pure(buf)
            ctx = ActiveContext(
                session_id=session_id,
                agent_name=agent_name,
                messages=msgs,
                token_estimate=sum(len(m.content) for m in msgs),
            )
            _log_state("get", session_id, agent_name, ctx.messages)
            return ctx
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
        if self._sdk is not None:
            from xana.memory._message_bridge import memory_message_to_sdk
            st = self._sdk._store.state(context.session_id)
            async with st.write_lock:
                buf = self._sdk._store.buffer(context.session_id, context.agent_name)
                buf.clear()
                for m in context.messages:
                    sdk_msg = memory_message_to_sdk(m)
                    if sdk_msg is not None:
                        buf.append(sdk_msg)

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
        await self._sdk_mirror_append_message(session_id, agent_name, message)
        return updated

    async def compact(self, session_id: str, agent_name: str, summary: str) -> ActiveContext:
        key = (session_id, agent_name)
        current = self._store.get(key) or ActiveContext(session_id=session_id, agent_name=agent_name)
        updated = replace(current, summary=summary, messages=())
        self._store[key] = updated
        self._snapshots[key] = []
        _log_state("compact", session_id, agent_name, ())
        if self._sdk is not None:
            st = self._sdk._store.state(session_id)
            async with st.write_lock:
                self._sdk._store.buffer(session_id, agent_name).clear()
        return updated

    async def clear(self, session_id: str, agent_name: str) -> None:
        """Wipe legacy + SDK buffers for (sid, agent).

        Bug A fix: before clearing the SDK buffer, scan for any
        CompactPlaceholder and release its compact_lock — otherwise a
        /clear during an in-flight compact would leak the lock and block
        the next /compact forever.
        Bug B fix: fire the ``on_clear`` callback (wired by boot) so
        harness-side per-(sid, agent) caches (``_session_prompt_tokens``)
        are popped. See docs/design/session-and-card-state.md §1.5.
        """
        self._store.pop((session_id, agent_name), None)
        self._snapshots.pop((session_id, agent_name), None)
        _log_state("clear", session_id, agent_name, ())
        if self._sdk is not None:
            from xana.context._compact_debug import log_lock
            from xana.context.message import CompactPlaceholder
            from xana.context.sdk import ContextSDK
            st = self._sdk._store.state(session_id)
            async with st.write_lock:
                buf = self._sdk._store.buffer(session_id, agent_name)
                for item in list(buf):
                    if isinstance(item, CompactPlaceholder):
                        log_lock("leak_release", session_id,
                                 item._internal.run_id, "store.clear")
                        ContextSDK._release_compact(st, item._internal.run_id)
                buf.clear()
        if self._on_clear is not None:
            try:
                self._on_clear(session_id, agent_name)
            except Exception:
                pass

    async def snapshot(
        self, session_id: str, agent_name: str
    ) -> tuple[MemoryMessage | Placeholder, ...]:
        if self._sdk is not None:
            from xana.memory._message_bridge import sdk_buffer_to_legacy_mixed
            buf = self._sdk_buffer(session_id, agent_name)
            if buf:
                snap = sdk_buffer_to_legacy_mixed(buf)
                _log_state("snapshot", session_id, agent_name, snap)
                return snap
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
        await self._sdk_mirror_append_placeholder(session_id, agent_name, placeholder)

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
                    await self._sdk_mirror_replace_placeholder(run_id, replace_with)
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
        await self._sdk_mirror_replace_range(
            session_id, agent_name, start_idx, end_idx, summary_message
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
