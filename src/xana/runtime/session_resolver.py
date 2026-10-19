"""SessionResolver: unified (adapter_id, thread_id) -> session_id map.

Canonical implementation per docs/design/session-and-card-state.md §1.0
(invariants I13–I17):

  * I13/I14 — every channel resolves session_id through this single map.
    No "default" sid path; cli uses ``thread_id=""``.
  * I15 — in-memory state is canonical. ``system.json`` is a best-effort
    mirror; disk write failures are logged but never raised so a wedged
    filesystem cannot block adapter callbacks.
  * I16 — ``system.json`` is read **once** at boot. The resolver never
    re-reads it; manual edits require a serve restart.
  * I17 — :meth:`rotate` is the /clear primitive. It mints a fresh sid
    and replaces the entry; the old sid's memory / active context / bg
    runs stay intact (this class doesn't own them).

Key format: ``f"{adapter_id}\\t{thread_id}"`` — tab delimited so chat_ids
containing ``:`` (telegram supergroups) don't collide with adapter_id.

session_id format: ``f"{adapter_id}-{uuid4().hex}"``. The adapter prefix
makes log/jsonl greps trivially channel-scoped.

Thread-safe: a single ``threading.Lock`` guards the map + mirror write.
Adapter callbacks land on multiple asyncio loops / executor threads, so
the lock is mandatory even though each op is O(1).
"""
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Callable

from xana.config.project_config import write_project_sessions
from xana.config.system_model import SystemConfig

_logger = logging.getLogger(__name__)

_WriterFn = Callable[[Path, SystemConfig], None]


def _make_key(adapter_id: str, thread_id: str) -> str:
    return f"{adapter_id}\t{thread_id}"


class SessionResolver:
    """The ONLY component that mints + persists session_ids.

    Parameters
    ----------
    system_config:
        Live ``SystemConfig`` instance — the resolver patches
        ``system_config.sessions`` in place before each mirror write so
        the serialised file always reflects the canonical map.
    system_json_path:
        Path to the project's ``config.json`` for mirror writes.
    writer:
        Optional injection point for tests; defaults to
        :func:`xana.config.project_config.write_project_sessions`, which writes only the
        session map (never the full config with credentials).
    """

    def __init__(
        self,
        *,
        system_config: SystemConfig,
        system_json_path: Path,
        writer: _WriterFn | None = None,
    ) -> None:
        self._system = system_config
        self._path = Path(system_json_path)
        self._writer = writer if writer is not None else write_project_sessions
        # Seed in-memory map from the SystemConfig snapshot (read-once at boot).
        self._map: dict[str, str] = dict(system_config.sessions)
        self._lock = threading.Lock()

    # -------- read-only ------------------------------------------------

    def current(self, adapter_id: str, thread_id: str) -> str | None:
        """Lookup-only; returns None if the key has never been resolved."""
        with self._lock:
            return self._map.get(_make_key(adapter_id, thread_id))

    def route_of(self, session_id: str) -> tuple[str, str] | None:
        """反查:当前映射到 ``session_id`` 的 (adapter_id, thread_id);没有返回 None。"""
        with self._lock:
            for key, sid in self._map.items():
                if sid == session_id:
                    adapter_id, _, thread_id = key.partition("\t")
                    return adapter_id, thread_id
        return None

    def snapshot(self) -> dict[str, str]:
        """Read-only copy of the canonical map (for tests / observability)."""
        with self._lock:
            return dict(self._map)

    # -------- mutations ------------------------------------------------

    def resolve(self, adapter_id: str, thread_id: str) -> str:
        """Return existing sid for the key, or mint + persist a new one."""
        key = _make_key(adapter_id, thread_id)
        with self._lock:
            existing = self._map.get(key)
            if existing is not None:
                return existing
            sid = self._mint(adapter_id)
            self._set_locked(key, sid)
            return sid

    def rotate(self, adapter_id: str, thread_id: str) -> str:
        """Mint a fresh sid for the key, replacing any existing entry.

        Old sid memory (active_context, memory tree, background runs) is
        the caller's concern — per I17 it stays live in process memory.
        """
        key = _make_key(adapter_id, thread_id)
        with self._lock:
            sid = self._mint(adapter_id)
            self._set_locked(key, sid)
            return sid

    # -------- internal -------------------------------------------------

    @staticmethod
    def _mint(adapter_id: str) -> str:
        return f"{adapter_id}-{uuid.uuid4().hex}"

    def _set_locked(self, key: str, sid: str) -> None:
        # Memory FIRST — canonical per I15. Mirror is best-effort.
        self._map[key] = sid
        self._system.sessions = dict(self._map)
        try:
            self._writer(self._path, self._system)
        except Exception as e:  # noqa: BLE001
            _logger.warning(
                "SessionResolver mirror write to %s failed (%s); "
                "memory holds %s=%s",
                self._path, e, key, sid,
            )
