"""In-memory ToolRegistry. Keyed by ComponentSpec.name."""
from __future__ import annotations

from xana.tools.contracts import ToolSpec, Toolset
from xana.tools.interfaces import Tool, ToolRegistry


class InMemoryToolRegistry(ToolRegistry):
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._toolsets: dict[str, Toolset] = {}

    async def register(self, tool: Tool) -> None:
        name = tool.spec().component.name
        self._tools[name] = tool

    async def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def list_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(t.spec() for t in self._tools.values())

    async def register_toolset(self, toolset: Toolset) -> None:
        self._toolsets[toolset.name] = toolset

    async def list_toolsets(self) -> tuple[Toolset, ...]:
        return tuple(self._toolsets.values())
