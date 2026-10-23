"""skill_load: 按名字取一个 bundled skill 的完整说明(SKILL 全文)。

CC 同款思路:每轮 prompt 只注入「# 可用技能」名字+描述列表
(SkillsListingContributor),模型按需调本工具拿全文 —— 零常驻 token。
构造注入独立的 bundled skills registry(不是 harness 的 skill_registry)。
"""
from __future__ import annotations

from pathlib import Path

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.skills.contracts import SkillActivation
from xana.skills.interfaces import SkillRegistry
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

_SPEC = ToolSpec(
    temporal="static",
    toolset="meta",
    component=ComponentSpec(
        kind="tool",
        name="skill_load",
        qualified_name="builtin.skill_load",
        description=(
            "按名字加载一个可用技能的完整说明(SKILL 全文)。"
            "「# 可用技能」段列出的技能匹配当前任务时,先调本工具拿完整说明再执行。"
        ),
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名,见「# 可用技能」列表"},
        },
        "required": ["name"],
    },
    read_only=True,
    display=ToolDisplay(category="other", verb="加载技能", summary_args=("name",)),
)


class SkillLoadTool(Tool):
    def __init__(self, *, skills: SkillRegistry) -> None:
        self.skills = skills

    def spec(self) -> ToolSpec:
        return _SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="skill_load 只读")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        name = str(invocation.input.get("name", "")).strip()
        activation = SkillActivation(
            kind="model",
            session_id=context.session_id,
            agent_name=context.agent_name,
            run_id=context.run_id or "",
            cwd=Path(context.cwd) if context.cwd else None,
        )
        visible = context.visible_skills
        skill = await self.skills.get(name) if visible is None or name in visible else None
        if skill is None:
            available = sorted(
                n for n in (s.spec().component.name for s in await self.skills.discover(activation))
                if visible is None or n in visible
            )
            listing = ", ".join(available) or "(无)"
            return ToolResult(
                ok=False,
                content=None,
                failure_kind="not_found",
                error=f"技能 {name!r} 不存在",
                rendered=f"[skill_load] 未找到技能 {name!r}。可用技能: {listing}",
            )
        contribution = await skill.activate(activation)
        rendered = "\n\n".join(contribution.prompt_blocks)
        return ToolResult(ok=True, content={"name": name}, rendered=rendered)
