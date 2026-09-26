"""Tool abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from xana.harness_sdk.contracts import HarnessContext
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import PermissionDecision, ToolContext, ToolInvocation, ToolResult, ToolSpec, Toolset


class ToolValidator(ABC):
    @abstractmethod
    async def validate(self, invocation: ToolInvocation) -> None: ...


class ToolPermissionPolicy(ABC):
    @abstractmethod
    async def decide(self, invocation: ToolInvocation, context: ToolContext) -> PermissionDecision: ...


class ToolSandboxPolicy(ABC):
    @abstractmethod
    async def prepare(self, invocation: ToolInvocation, context: ToolContext) -> ToolContext: ...

    @abstractmethod
    async def cleanup(self, invocation: ToolInvocation, context: ToolContext) -> None: ...


class ToolContextBuilder(ABC):
    @abstractmethod
    async def build(self, invocation: ToolInvocation, context: HarnessContext) -> ToolContext: ...


class ToolExecutor(ABC):
    @abstractmethod
    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult: ...


class ToolProgressStream(ABC):
    @abstractmethod
    def stream(self, invocation_id: str) -> AsyncIterator[dict]: ...


class ToolRenderer(ABC):
    @abstractmethod
    async def render(self, result: ToolResult) -> str: ...


class Tool(ABC):
    @abstractmethod
    def spec(self) -> ToolSpec: ...

    @abstractmethod
    async def validate(self, invocation: ToolInvocation) -> None: ...

    @abstractmethod
    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult: ...

    @abstractmethod
    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation: ...

    @abstractmethod
    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult: ...

    @abstractmethod
    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult: ...

    @abstractmethod
    async def render_result(self, result: ToolResult) -> str: ...

    @abstractmethod
    async def cancel(self, call_id: str) -> None: ...


class ToolRegistry(ABC):
    @abstractmethod
    async def register(self, tool: Tool) -> None: ...

    @abstractmethod
    async def get(self, name: str) -> Tool | None: ...

    @abstractmethod
    async def list_specs(self) -> tuple[ToolSpec, ...]: ...

    @abstractmethod
    async def register_toolset(self, toolset: Toolset) -> None: ...

    @abstractmethod
    async def list_toolsets(self) -> tuple[Toolset, ...]: ...
