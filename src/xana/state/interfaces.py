"""State abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod

from xana.state.contracts import Checkpoint, ResumeDecision, RunRecord, RunStatus, SessionRecord, UsageSnapshot


class SessionStore(ABC):
    @abstractmethod
    async def get(self, session_id: str) -> SessionRecord | None: ...

    @abstractmethod
    async def upsert(self, record: SessionRecord) -> None: ...

    @abstractmethod
    async def patch(self, session_id: str, fields: dict) -> SessionRecord: ...

    @abstractmethod
    async def list(self) -> tuple[SessionRecord, ...]: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...


class RunStore(ABC):
    @abstractmethod
    async def create(self, record: RunRecord) -> None: ...

    @abstractmethod
    async def get(self, run_id: str) -> RunRecord | None: ...

    @abstractmethod
    async def update_status(self, run_id: str, status: RunStatus, fields: dict | None = None) -> RunRecord: ...

    @abstractmethod
    async def checkpoint(self, run_id: str, checkpoint: Checkpoint) -> None: ...

    @abstractmethod
    async def update_usage(self, run_id: str, usage: UsageSnapshot) -> None: ...

    @abstractmethod
    async def list_unfinished(self) -> tuple[RunRecord, ...]: ...

    @abstractmethod
    async def list_by_session(self, session_id: str) -> tuple[RunRecord, ...]: ...


class ResumePolicy(ABC):
    @abstractmethod
    async def decide(self, run: RunRecord) -> ResumeDecision: ...


class UsageEstimator(ABC):
    @abstractmethod
    async def estimate(self, messages: tuple[dict, ...], model: str) -> UsageSnapshot: ...


class StateRepository(ABC):
    @abstractmethod
    def sessions(self) -> SessionStore: ...

    @abstractmethod
    def runs(self) -> RunStore: ...
