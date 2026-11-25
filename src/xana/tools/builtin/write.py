"""Write: create or overwrite a file with provided content."""
from __future__ import annotations

from pathlib import Path

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.policy.path_policy import ProtectedDirsPathPolicy
from xana.tools.builtin._path import resolve_path
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_SPEC = ToolSpec(
    temporal="static",
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
        protected_dir: "Path | tuple[Path, ...] | list[Path] | None" = None,
    ) -> None:
        # protected_dir: 受保护目录(记忆目录 memory/ / 两级 agent 文档目录)—— 裸写被 policy 拒绝;
        # 记忆唯一写入口是 memory_write,agent 文档是 agent_define。
        if protected_dir is None:
            self._protected_dirs: tuple[Path, ...] = ()
        elif isinstance(protected_dir, Path):
            self._protected_dirs = (protected_dir.resolve(),)
        else:
            self._protected_dirs = tuple(Path(d).resolve() for d in protected_dir)
        self._path_policy = ProtectedDirsPathPolicy(
            protected_dirs=self._protected_dirs,
            deny_reason=(
                "目标位于受保护目录,裸 Write 被拒绝:"
                "记忆(memory/)只能用 memory_write 写(action=create/update/delete),"
                "agent 文档(AGENT.md)只能用 agent_define 写。"
            ),
        )

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not isinstance(invocation.input.get("file_path"), str) or not invocation.input["file_path"]:
            raise ValueError("Write requires non-empty 'file_path'")
        if "content" not in invocation.input:
            raise ValueError("Write requires 'content'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        res = self._path_policy.check_path(Path(invocation.input["file_path"]), operation="write", cwd=Path(context.cwd))
        if res.decision != "allow":
            return res
        return PermissionResult(decision="allow", reason="file write allowed")

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
