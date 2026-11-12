"""EnterWorktree: create a git worktree under <worktree_root>/{sid}/{name}.

cc's analog spawns a worktree off the current branch to isolate experimental
changes. We persist the worktree path on SessionConfig.metadata so subsequent
turns can resolve cwd against it.

Rejects names containing '..' or path separators. The CALLER (model) is
expected to commit before invoking — we do NOT auto-snapshot dirty state.
"""
from __future__ import annotations

import asyncio
import dataclasses
import re
import secrets
from pathlib import Path

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_SPEC = ToolSpec(
    temporal="static",
    toolset="session",
    component=ComponentSpec(
        kind="tool",
        name="EnterWorktree",
        qualified_name="builtin.EnterWorktree",
        description="Create a git worktree off HEAD; subsequent turns use it as the working directory.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "worktree name (letters/digits/._-, max 64). Random if omitted.",
            },
        },
    },
    destructive=True,
    display=ToolDisplay(category="other", verb="切换工作树", summary_args=("name", "path")),
)


class EnterWorktreeTool(Tool):
    def __init__(self, *, session_config_store, worktree_root: Path) -> None:
        self.session_config_store = session_config_store
        self.worktree_root = Path(worktree_root)

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        name = invocation.input.get("name")
        if name is None:
            return
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"EnterWorktree: bad name {name!r}")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="worktree creation allowed")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        name = invocation.input.get("name") or f"wt-{secrets.token_hex(4)}"
        sid = context.session_id
        if not sid or "/" in sid or "\\" in sid or sid in (".", ".."):
            return ToolResult(
                ok=False,
                content=None,
                error=f"EnterWorktree: unsafe session_id {context.session_id!r}",
                failure_kind="execution",
                rendered=f"[EnterWorktree FAILED] unsafe session_id {context.session_id!r}",
            )
        target = self.worktree_root / context.session_id / name
        target.parent.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", context.cwd, "worktree", "add", str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace").strip() or "git worktree add failed"
            return ToolResult(
                ok=False,
                content=None,
                error=err_msg,
                failure_kind="execution",
                rendered=f"[EnterWorktree FAILED] {err_msg}",
            )

        if self.session_config_store is not None:
            current = await self.session_config_store.get(context.session_id)
            updated = dataclasses.replace(
                current,
                metadata={**current.metadata, "worktree_path": str(target)},
            )
            await self.session_config_store.save(updated)

        return ToolResult(
            ok=True,
            content={"worktree_path": str(target)},
            rendered=f"[EnterWorktree ok] {target}（从下一轮起生效）",
            metadata={"worktree_path": str(target)},
        )
