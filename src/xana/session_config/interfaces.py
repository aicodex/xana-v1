"""SessionConfigStore: persistence boundary for per-session runtime knobs.

Read by the CLI/REPL before each turn; written by slash-command handlers that
mutate config (/model, /effort, /streaming, /thinking, /search_provider).

Implementations must be safe to call concurrently from multiple REPL processes
sharing the same session_id (treat the storage path as authoritative — re-read
on every get).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from xana.session_config.contracts import SessionConfig


class SessionConfigStore(ABC):
    @abstractmethod
    async def get(self, session_id: str) -> SessionConfig: ...

    @abstractmethod
    async def save(self, config: SessionConfig) -> None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...
