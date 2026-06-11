"""Hook abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod

from xana.hooks.contracts import HookInput, HookKind, HookResult, HookSpec


class Hook(ABC):
    @abstractmethod
    def spec(self) -> HookSpec: ...

    @abstractmethod
    async def run(self, input: HookInput) -> HookResult: ...


class PreTurnHook(Hook, ABC):
    pass


class PostTurnHook(Hook, ABC):
    pass


class PreModelRequestHook(Hook, ABC):
    pass


class PostModelResponseHook(Hook, ABC):
    pass


class PreToolUseHook(Hook, ABC):
    pass


class PostToolUseHook(Hook, ABC):
    pass


class HookRegistry(ABC):
    @abstractmethod
    async def register(self, hook: Hook) -> None: ...

    @abstractmethod
    async def list(self, kind: HookKind | None = None) -> tuple[HookSpec, ...]: ...

    @abstractmethod
    async def resolve(self, kind: HookKind) -> tuple[Hook, ...]: ...


class HookRunner(ABC):
    @abstractmethod
    async def run_all(self, kind: HookKind, input: HookInput) -> tuple[HookResult, ...]: ...
