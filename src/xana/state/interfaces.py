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
    async def list_unfinished(self) -> tuple[RunRecord, ...]:
        """返回可恢复(/resume 候选)的 run 记录。

        过滤规则(P5 tightened):
          - 仅 exit_reason 在
            {"user_cancel","tool_kill_timeout","shutdown_graceful","shutdown_force"}
            的记录进入候选。
          - 其它(包括 status==running/paused 但无 exit_reason 的孤儿)一律排除。
            孤儿应由 startup_scan 打上 shutdown_force 后再出现在这里。
        """
        ...

    @abstractmethod
    async def list_by_session(self, session_id: str) -> tuple[RunRecord, ...]: ...

    @abstractmethod
    async def list_all(self, *, status: str | None = None) -> tuple[RunRecord, ...]:
        """返回全部记录(§2.2 已批准上 ABC)。

        消费者:任务面板(§4.4)+ shutdown 遗留扫描。status 非 None 时按
        RunRecord.status 过滤(如 status="running" 取在途集合)。
        """
        ...


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
