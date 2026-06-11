"""Component abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from xana.component.contracts import ComponentKind, ComponentSpec, LoadCacheEntry, LoadCacheKey, LoadOutcome, SourcePriorityPolicy


class Component(ABC):
    @abstractmethod
    def spec(self) -> ComponentSpec: ...


class ComponentLoader(ABC):
    @abstractmethod
    async def load_root(self, root: Path) -> LoadOutcome: ...

    @abstractmethod
    async def load_all(self, roots: tuple[Path, ...]) -> LoadOutcome: ...


class ComponentValidator(ABC):
    @abstractmethod
    async def validate(self, spec: ComponentSpec) -> None: ...


class ComponentRegistry(ABC):
    @abstractmethod
    async def register(self, component: Component) -> None: ...

    @abstractmethod
    async def get(self, qualified_name: str) -> Component | None: ...

    @abstractmethod
    async def list(self, kind: ComponentKind | None = None) -> tuple[ComponentSpec, ...]: ...

    @abstractmethod
    async def remove(self, qualified_name: str) -> None: ...


class SourcePriorityResolver(ABC):
    @abstractmethod
    async def priority(self) -> SourcePriorityPolicy: ...


class OverrideResolver(ABC):
    @abstractmethod
    async def resolve(self, specs: tuple[ComponentSpec, ...]) -> LoadOutcome: ...


class LoadCache(ABC):
    @abstractmethod
    async def get(self, key: LoadCacheKey) -> LoadCacheEntry | None: ...

    @abstractmethod
    async def put(self, entry: LoadCacheEntry) -> None: ...

    @abstractmethod
    async def invalidate(self, key: LoadCacheKey) -> None: ...
