"""Config abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from xana.config.contracts import ConfigSource, RawConfig, RuntimeConfig


class ConfigReader(ABC):
    @abstractmethod
    async def read(self, source: ConfigSource) -> RawConfig: ...


class ConfigMerger(ABC):
    @abstractmethod
    async def merge(self, configs: tuple[RawConfig, ...]) -> RuntimeConfig: ...


class ConfigValidator(ABC):
    @abstractmethod
    async def validate(self, config: RuntimeConfig) -> None: ...


class ConfigProvider(ABC):
    @abstractmethod
    async def load(self, project_root: Path) -> RuntimeConfig: ...


class ConfigWatcher(ABC):
    @abstractmethod
    async def changed(self) -> bool: ...
