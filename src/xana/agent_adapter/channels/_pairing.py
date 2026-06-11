# src/xana/agent_adapter/channels/_pairing.py
"""PairingStore: per-platform user allowlist + pending queue.

JSON sidecar at ~/.xana/channels/pairings.json, atomic writes, mtime-based
hot reload so a long-running `xana serve` sees approvals made by the
`xana pair` CLI in another process. Mirrors yuxu_backup's PairingRegistry
semantics but stores JSON (xana convention) instead of YAML.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PairingStore:
    def __init__(self, *, path: Path) -> None:
        self._path = path
        self._fingerprint: tuple[int, int] | None = None  # (mtime_ns, st_size)
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._reload_if_changed()

    # Queries -----------------------------------------------------------

    def is_allowed(self, platform: str, user_id: str) -> bool:
        self._reload_if_changed()
        return user_id in self._platform(platform)["allowed"]

    def list_allowed(self, platform: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        return self._list("allowed", platform)

    def list_pending(self, platform: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        return self._list("pending", platform)

    # Mutations (each reloads first so concurrent writers don't clobber) -

    def add_pending(
        self, platform: str, user_id: str, *, first_message: str = "", chat_id: str | None = None
    ) -> bool:
        self._reload_if_changed()
        bucket = self._platform(platform)
        if user_id in bucket["allowed"] or user_id in bucket["pending"]:
            return False
        bucket["pending"][user_id] = {
            "first_seen": _now(),
            "first_message": first_message[:200],
            "chat_id": chat_id,
        }
        self._save()
        return True

    def approve(self, platform: str, user_id: str, *, note: str = "") -> bool:
        self._reload_if_changed()
        bucket = self._platform(platform)
        if user_id not in bucket["pending"]:
            return False
        del bucket["pending"][user_id]
        bucket["allowed"][user_id] = {"approved_at": _now(), "note": note}
        self._save()
        return True

    def reject(self, platform: str, user_id: str) -> bool:
        self._reload_if_changed()
        bucket = self._platform(platform)
        if user_id not in bucket["pending"]:
            return False
        del bucket["pending"][user_id]
        self._save()
        return True

    def revoke(self, platform: str, user_id: str) -> bool:
        self._reload_if_changed()
        bucket = self._platform(platform)
        if user_id not in bucket["allowed"]:
            return False
        del bucket["allowed"][user_id]
        self._save()
        return True

    def allow(self, platform: str, user_id: str, *, note: str = "") -> None:
        """Directly allowlist (admin self-pair during setup)."""
        self._reload_if_changed()
        bucket = self._platform(platform)
        bucket["pending"].pop(user_id, None)
        bucket["allowed"][user_id] = {"approved_at": _now(), "note": note}
        self._save()

    # Internals ----------------------------------------------------------

    def _platform(self, platform: str) -> dict[str, dict[str, Any]]:
        return self._data.setdefault(platform, {"allowed": {}, "pending": {}})

    def _list(self, kind: str, platform: str | None) -> list[tuple[str, dict[str, Any]]]:
        self._reload_if_changed()
        out: list[tuple[str, dict[str, Any]]] = []
        for plat, bucket in sorted(self._data.items()):
            if platform is not None and plat != platform:
                continue
            for user_id, entry in bucket.get(kind, {}).items():
                out.append((plat, {"user_id": user_id, **entry}))
        return out

    def _reload_if_changed(self) -> None:
        try:
            st = self._path.stat()
        except FileNotFoundError:
            self._data = {}
            self._fingerprint = None
            return
        fingerprint = (st.st_mtime_ns, st.st_size)
        if fingerprint == self._fingerprint:
            return
        # Record fingerprint *before* reading so a concurrent write between
        # stat and read forces a reload on the next call (mtime/size may differ).
        self._fingerprint = fingerprint
        self._data = json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.parent / (self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
        try:
            self._path.chmod(0o600)
        except OSError:
            pass
        st = self._path.stat()
        self._fingerprint = (st.st_mtime_ns, st.st_size)
