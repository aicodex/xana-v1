"""In-memory ToolRegistry. Keyed by ComponentSpec.name."""
from __future__ import annotations

from xana.tools.contracts import ToolSpec
from xana.tools.interfaces import Tool, ToolRegistry


class InMemoryToolRegistry(ToolRegistry):
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    async def register(self, tool: Tool) -> None:
        name = tool.spec().component.name
        self._tools[name] = tool

    async def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def list_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(t.spec() for t in self._tools.values())
