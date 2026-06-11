"""Runtime abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod

from xana.runtime.contracts import BootRequest, RuntimeHealth, TaskHandle, TaskRequest, TaskState


class ComponentLifecycle(ABC):
    @abstractmethod
    async def install(self) -> None: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def uninstall(self) -> None: ...


class BootSequence(ABC):
    @abstractmethod
    async def load_config(self, request: BootRequest) -> None: ...

    @abstractmethod
    async def restore_memory(self) -> None: ...

    @abstractmethod
    async def build_components(self) -> None: ...

    @abstractmethod
    async def start_adapters(self) -> None: ...


class Scheduler(ABC):
    @abstractmethod
    async def spawn(self, request: TaskRequest) -> TaskHandle: ...

    @abstractmethod
    async def cancel(self, task_id: str) -> None: ...

    @abstractmethod
    async def status(self, task_id: str) -> TaskState: ...


class Runtime(ABC):
    @abstractmethod
    async def boot(self, request: BootRequest) -> RuntimeHealth: ...

    @abstractmethod
    async def shutdown(self) -> RuntimeHealth: ...

    @abstractmethod
    async def health(self) -> RuntimeHealth: ...
