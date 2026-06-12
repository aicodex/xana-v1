"""src/xana/extension_api/interfaces.py

The 8 Contributor ABCs. Lifecycle contributors ship concrete no-op defaults
(subclasses override only what they need); data-producing contributors are
abstract (a no-op implementation would be meaningless).

Design note: contribute_prompt / contribute_turn_input have distinct names so
one class can implement both (yuxu's reference design used `contribute` twice).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from xana.extension_api.contracts import (
    ApprovalReviewInput,
    ContextFragment,
    ExtensionData,
    PromptFragment,
    ThreadStartInput,
    ThreadStopInput,
    ToolFinishInput,
    ToolStartInput,
    TurnAbortInput,
    TurnInputContext,
    TurnStartInput,
    TurnStopInput,
)
from xana.policy.contracts import PermissionResult
from xana.tools.interfaces import Tool


class ContextContributor(ABC):
    """Contributes system-prompt fragments, assembled by PromptAssembler."""

    @abstractmethod
    async def contribute_prompt(
        self, thread_store: ExtensionData, turn_store: ExtensionData
    ) -> list[PromptFragment]: ...


class TurnInputContributor(ABC):
    """Contributes per-turn context fragments (injected as system-tail blocks)."""

    @abstractmethod
    async def contribute_turn_input(
        self,
        ctx: TurnInputContext,
        thread_store: ExtensionData,
        turn_store: ExtensionData,
    ) -> list[ContextFragment]: ...


class ToolContributor(ABC):
    """Declares tools this extension owns; merged into the run's tool schema."""

    @abstractmethod
    def tools(self) -> list[Tool]: ...


class ToolLifecycleContributor(ABC):
    async def on_tool_start(self, input: ToolStartInput) -> None:
        return None

    async def on_tool_finish(self, input: ToolFinishInput) -> None:
        return None


class TurnLifecycleContributor(ABC):
    async def on_turn_start(self, input: TurnStartInput) -> None:
        return None

    async def on_turn_stop(self, input: TurnStopInput) -> None:
        return None

    async def on_turn_abort(self, input: TurnAbortInput) -> None:
        return None


class ThreadLifecycleContributor(ABC):
    async def on_thread_start(self, input: ThreadStartInput) -> None:
        return None

    async def on_thread_stop(self, input: ThreadStopInput) -> None:
        return None


class ConfigContributor(ABC):
    async def on_config_changed(
        self, previous: dict, new: dict, thread_store: ExtensionData
    ) -> None:
        return None


class ApprovalReviewContributor(ABC):
    """May override a non-allow PermissionResult. Return None to abstain."""

    @abstractmethod
    async def review(self, input: ApprovalReviewInput) -> PermissionResult | None: ...
