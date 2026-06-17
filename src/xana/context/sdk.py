"""ContextSDK facade (§3 + §1.5).

Single entry point for all context reads/writes. Backed by a private
in-memory ``_ContextStore``. Per-sid locks serialize writes; a
per-sid compact lock + event enforce §1.5 constraints 3 + 4.
"""
from __future__ import annotations

from typing import Sequence

from xana.context import _internal as _ctx_internal  # noqa: F401 — smoke-tests guard
from xana.context._internal import _PlaceholderInternal
from xana.context._store import _ContextStore
from xana.context.message import (
    CompactPlaceholder,
    Message,
    RegularPlaceholder,
    UserMessage,
)

del _ctx_internal


class ContextSDK:
    """§3 facade — sole legal entry point for context state."""

    def __init__(self) -> None:
        self._store = _ContextStore()

    # ------------------------------------------------------------------
    # Append APIs
    # ------------------------------------------------------------------

    async def append_user(self, sid: str, agent: str, text: str) -> UserMessage:
        st = self._store.state(sid)
        async with st.write_lock:
            msg = UserMessage(text=text)
            self._store.buffer(sid, agent).append(msg)
            return msg

    async def append_regular_placeholder(
        self, sid: str, agent: str, label: str, run_id: str, ph_id: str
    ) -> RegularPlaceholder:
        st = self._store.state(sid)
        async with st.write_lock:
            ph = RegularPlaceholder(
                label=label,
                _internal=_PlaceholderInternal(
                    run_id=run_id, ph_id=ph_id, transcript_path=""
                ),
            )
            self._store.buffer(sid, agent).append(ph)
            return ph

    async def append_compact_placeholder(
        self,
        sid: str,
        agent: str,
        run_id: str,
        ph_id: str,
        transcript_path: str,
    ) -> CompactPlaceholder:
        """Acquire the per-sid compact lock (§1.5 c4), insert PH before
        the earliest RegularPlaceholder (§1.5 c2), and clear the
        compact-replaced event so snapshot_for_fork() will block.

        Lock + event are released/set by ``replace_placeholder`` or
        ``cancel_compact_placeholder``.
        """
        st = self._store.state(sid)
        # Block until any in-flight compact is fully replaced (c4).
        await st.compact_lock.acquire()
        try:
            async with st.write_lock:
                buf = self._store.buffer(sid, agent)
                ph = CompactPlaceholder(
                    _internal=_PlaceholderInternal(
                        run_id=run_id,
                        ph_id=ph_id,
                        transcript_path=transcript_path,
                    )
                )
                idx = self._store.earliest_regular_index(buf)
                if idx < 0:
                    buf.append(ph)
                else:
                    buf.insert(idx, ph)
                # Mark a pending compact: future fork must wait.
                st.compact_event.clear()
                st.compact_holder = run_id
                return ph
        except BaseException:
            # Failed to record PH — don't hold the lock.
            if st.compact_lock.locked():
                st.compact_lock.release()
            st.compact_event.set()
            st.compact_holder = ""
            raise

    # ------------------------------------------------------------------
    # Replace / cancel
    # ------------------------------------------------------------------

    async def replace_placeholder(
        self,
        sid: str,
        agent: str,
        run_id: str,
        replacements: Sequence[Message],
    ) -> None:
        st = self._store.state(sid)
        async with st.write_lock:
            buf = self._store.buffer(sid, agent)
            idx = self._store.find_by_run_id(buf, run_id)
            if idx < 0:
                return
            existing = buf[idx]
            buf[idx : idx + 1] = list(replacements)
            # If we replaced a compact PH, release the compact lock and
            # signal blocked fork callers.
            if isinstance(existing, CompactPlaceholder):
                self._release_compact(st, run_id)

    async def cancel_compact_placeholder(
        self, sid: str, agent: str, run_id: str
    ) -> None:
        """Equivalent to replace_placeholder(run_id, ()) for a compact PH."""
        await self.replace_placeholder(sid, agent, run_id, ())

    @staticmethod
    def _release_compact(st, run_id: str) -> None:
        # Only release if we're the holder; idempotent for safety.
        if st.compact_holder == run_id:
            st.compact_holder = ""
        st.compact_event.set()
        if st.compact_lock.locked():
            st.compact_lock.release()

    # ------------------------------------------------------------------
    # Snapshot (fork)
    # ------------------------------------------------------------------

    async def snapshot_for_fork(self, sid: str, agent: str) -> list[Message]:
        """§1.5 c3: if a CompactPlaceholder sits before the earliest
        RegularPlaceholder and has not been replaced, block until it is.
        """
        st = self._store.state(sid)
        while True:
            buf = self._store.buffer(sid, agent)
            comp_idx = self._store.find_compact_placeholder_index(buf)
            reg_idx = self._store.earliest_regular_index(buf)
            blocked = comp_idx >= 0 and (reg_idx < 0 or comp_idx < reg_idx)
            if not blocked:
                return list(buf)
            await st.compact_event.wait()
            # loop and re-check (event could have been set, then a new
            # compact appended).

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_anthropic(
        self, sid: str, agent: str, *, system_content: str
    ) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system_content}]
        for m in self._store.buffer(sid, agent):
            out.extend(m.to_anthropic())
        return out

    def to_openai(
        self, sid: str, agent: str, *, system_content: str
    ) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system_content}]
        for m in self._store.buffer(sid, agent):
            out.extend(m.to_openai())
        return out
