"""src/xana/extension_api/capabilities.py

Runtime Service capability ABCs. Services are harness/runtime-provided and
constructor-injected into extensions (Rule 5: no global singletons).
Phase 1 defines the interfaces only; implementations come with consumers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SchedulerService(ABC):
    @abstractmethod
    async def add(self, *, cron: str, prompt: str, recurring: bool = False) -> str: ...

    @abstractmethod
    async def remove(self, task_id: str) -> None: ...

    @abstractmethod
    async def list_all(self) -> tuple[dict[str, Any], ...]: ...


class SandboxManager(ABC):
    @abstractmethod
    async def run_subprocess(
        self, command: list[str], *, cwd: str | None = None, timeout_s: float | None = None
    ) -> tuple[int, str, str]:
        """Returns (exit_code, stdout, stderr)."""


class AgentSpawner(ABC):
    @abstractmethod
    async def spawn(self, *, agent_name: str, prompt: str, session_id: str) -> str:
        """Spawn a new agent run attached to session_id (the caller-provided session).

        session_id is the existing session the spawned run joins; returns the
        new run_id assigned to the spawned run.
        """
