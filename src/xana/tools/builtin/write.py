"""Write: create or overwrite a file with provided content."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.builtin._path import resolve_path
from xana.tools.builtin._reindex import maybe_reindex_markdown
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

if TYPE_CHECKING:
    from xana.memory.markdown import MarkdownMemoryTree


_SPEC = ToolSpec(
    toolset="fs",
    component=ComponentSpec(
        kind="tool",
        name="Write",
        qualified_name="builtin.Write",
        description="Create a new file or overwrite an existing file with the given content.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["file_path", "content"],
    },
    destructive=True,
    display=ToolDisplay(category="edit", verb="修改", unit="个文件", summary_args=("file_path",), result="none"),
)


class WriteTool(Tool):
    def __init__(
        self,
        *,
        memory_tree: MarkdownMemoryTree | None = None,
        protected_dir: "Path | tuple[Path, ...] | list[Path] | None" = None,
    ) -> None:
        # memory_tree: 成功写入 root 内 .md 后增量 reindex（L1 联动）。
        # protected_dir: 受保护目录(L2 规则目录 / ~/.xana/agents Agent 文档目录,
        # §6.3)—— 裸写被 policy 拒绝;规则唯一写入口是 memory_write 工具,
        # Agent 文档唯一变更路径是 episode 提案→评估→判决闭环。
        self._memory_tree = memory_tree
        if protected_dir is None:
            self._protected_dirs: tuple[Path, ...] = ()
        elif isinstance(protected_dir, Path):
            self._protected_dirs = (protected_dir.resolve(),)
        else:
            self._protected_dirs = tuple(Path(d).resolve() for d in protected_dir)

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not isinstance(invocation.input.get("file_path"), str) or not invocation.input["file_path"]:
            raise ValueError("Write requires non-empty 'file_path'")
        if "content" not in invocation.input:
            raise ValueError("Write requires 'content'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        target = resolve_path(invocation.input["file_path"], context.cwd).resolve()
        for protected in self._protected_dirs:
            try:
                target.relative_to(protected)
            except ValueError:
                continue
            return PermissionResult(
                decision="deny",
                reason=(
                    f"{target} 位于受保护目录,裸 Write 被拒绝;"
                    "规则的唯一写入口是 memory_write 工具"
                    "(action=create/refine/supersede);"
                    "Agent 文档的唯一变更路径是 episode 提案→评估→判决闭环(§6.3)。"
                ),
            )
        return PermissionResult(decision="allow", reason="file write allowed")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        path = resolve_path(invocation.input["file_path"], context.cwd)
        content = str(invocation.input["content"])
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        action = "overwritten" if existed else "created"
        return ToolResult(
            ok=True,
            content=content,
            rendered=f"[Write ok] {action} {path} ({len(content)} bytes)",
        )

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        if result.ok and self._memory_tree is not None:
            await maybe_reindex_markdown(
                self._memory_tree, invocation.input["file_path"], context.cwd
            )
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
