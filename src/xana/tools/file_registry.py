"""FileToolRegistry —— 工具目录:内置 / 扩展登记的工具 + 两级目录的 TOOL.md 派生工具,热加载
(docs/design/iteration-agent-design.md §5.3,与 FileAgentRegistry 对称)。

- 目录按优先级从低到高:用户级 {XANA_HOME}/tools、项目级 <project_root>/.xana/tools;同名时项目级覆盖。
- 登记的工具名是保留名,TOOL.md 不能覆盖。
- get / list_specs 前按需比对文件 (修改时间, 大小) 重新加载(至多每 min_interval_s 一次);
  改坏的文档保留上一个可用版本,删除的文档移出目录。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from pathlib import Path

from xana.component.contracts import ComponentScope
from xana.skills.markdown_loader import SkillManifestError, split_frontmatter
from xana.tools.command_tool import TOOL_DOC_NAME, CommandTool, ToolDocError, parse_tool_doc
from xana.tools.contracts import ToolSpec, Toolset
from xana.tools.interfaces import Tool, ToolRegistry

_logger = logging.getLogger(__name__)


def load_tool_doc(path: Path, scope: ComponentScope, project_root: Path) -> CommandTool:
    try:
        fm, body = split_frontmatter(Path(path).read_text(encoding="utf-8"), path=Path(path))
    except SkillManifestError as exc:
        raise ToolDocError(path, exc.field, exc.reason) from exc
    return CommandTool(parse_tool_doc(fm, body, path=Path(path)), path=Path(path), scope=scope,
                       project_root=project_root)


class FileToolRegistry(ToolRegistry):
    def __init__(self, *, roots: Sequence[tuple[ComponentScope, Path]], project_root: Path,
                 min_interval_s: float = 2.0) -> None:
        self._roots = tuple((scope, Path(root)) for scope, root in roots)
        self._project_root = Path(project_root)
        self._min_interval_s = min_interval_s
        self._registered: dict[str, Tool] = {}
        self._toolsets: dict[str, Toolset] = {}
        self._parsed: dict[Path, tuple[tuple[int, int], CommandTool]] = {}
        self._files: dict[str, CommandTool] = {}           # 当前生效的派生工具
        self._reported: set[tuple[Path, tuple[int, int]]] = set()
        self._last_scan = float("-inf")
        self._lock = asyncio.Lock()

    @property
    def roots(self) -> dict[str, Path]:
        return {scope: root for scope, root in self._roots}

    async def register(self, tool: Tool) -> None:
        self._registered[tool.spec().component.name] = tool

    async def get(self, name: str) -> Tool | None:
        await self.refresh()
        return self._registered.get(name) or self._files.get(name)

    async def list_specs(self) -> tuple[ToolSpec, ...]:
        await self.refresh()
        return tuple(t.spec() for t in (*self._registered.values(), *self._files.values()))

    async def register_toolset(self, toolset: Toolset) -> None:
        self._toolsets[toolset.name] = toolset

    async def list_toolsets(self) -> tuple[Toolset, ...]:
        return tuple(self._toolsets.values())

    async def file_tools(self) -> tuple[CommandTool, ...]:
        await self.refresh()
        return tuple(self._files.values())

    async def refresh(self, *, force: bool = False) -> tuple[ToolDocError, ...]:
        """同步派生工具与磁盘;返回本次新出现的解析错误(同一版本的坏文件只报一次)。"""
        if not force and time.monotonic() - self._last_scan < self._min_interval_s:
            return ()
        async with self._lock:
            self._last_scan = time.monotonic()
            return self._sync()

    def _sync(self) -> tuple[ToolDocError, ...]:
        errors: list[ToolDocError] = []
        chosen: dict[str, CommandTool] = {}
        seen: set[Path] = set()
        for scope, root in self._roots:                     # 低 → 高,后写覆盖
            if not root.is_dir():
                continue
            for path in sorted(root.glob(f"*/{TOOL_DOC_NAME}")):
                try:
                    st = path.stat()
                except OSError:
                    continue
                version = (st.st_mtime_ns, st.st_size)
                seen.add(path)
                cached = self._parsed.get(path)
                if cached is None or cached[0] != version:
                    try:
                        self._parsed[path] = (version, load_tool_doc(path, scope, self._project_root))
                    except (ToolDocError, OSError) as err:
                        if (path, version) not in self._reported:
                            self._reported.add((path, version))
                            errors.append(err if isinstance(err, ToolDocError) else ToolDocError(path, "file", str(err)))
                        if cached is None:
                            continue
                cur_version, tool = self._parsed[path]
                name = tool.spec().component.name
                if name in self._registered:
                    if (path, cur_version) not in self._reported:
                        self._reported.add((path, cur_version))
                        errors.append(ToolDocError(path, "name", f"{name} 是内置工具,不能被 TOOL.md 覆盖"))
                    continue
                chosen[name] = tool
        for path in [p for p in self._parsed if p not in seen]:
            del self._parsed[path]
        self._files = chosen
        for err in errors:
            _logger.warning("TOOL.md 加载失败: %s", err)
        return tuple(errors)

    async def remove(self, name: str) -> Path:
        """删除派生工具(当前生效的 TOOL.md);内置或不存在抛 LookupError。"""
        await self.refresh(force=True)
        tool = self._files.get(name)
        if tool is None:
            raise LookupError(f"没有名为 {name!r} 的派生工具(内置工具不能删除)")
        path = tool.spec().component.source.path
        assert path is not None
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        await self.refresh(force=True)
        return path
