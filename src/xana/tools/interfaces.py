"""Tool abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec, Toolset, ToolTemporal


class Tool(ABC):
    """工具。必须实现 spec / check_permission / execute;其余钩子给默认实现(无额外校验、原样透传),
    需要时覆盖。check_permission 保持抽象:每个工具都要明确表态。"""

    @abstractmethod
    def spec(self) -> ToolSpec: ...

    async def validate(self, invocation: ToolInvocation) -> None:
        """输入 schema 之外的业务校验;失败抛 ValueError。"""
        return None

    @abstractmethod
    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult: ...

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    @abstractmethod
    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult: ...

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None

    def temporal_for(self, invocation: ToolInvocation) -> ToolTemporal | None:
        """本次调用结果与时间的关系;缺省 = spec().temporal。按参数而异的工具(如部分子命令能按时点截断)覆盖。"""
        return self.spec().temporal


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


class ToolEnvironment(ABC):
    """评测会话的工具环境(iteration-agent-design §6):harness 在统一入口把每次工具调用交给它,
    由它决定回放录制、真实执行(时点经 ToolContext.env 注入)、拒绝或模拟,并在结果 metadata["env"]
    记下所用策略与是否回退。只在 ToolContext.env 有值的 run 上调用。"""

    @abstractmethod
    async def execute(self, invocation: ToolInvocation, tool: Tool, context: ToolContext,
                      run: Callable[[], Awaitable[ToolResult]]) -> ToolResult:
        """run() 是真实执行(pre_tool_use → execute → post_tool_use)。"""
