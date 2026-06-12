"""loop skill — 周期性执行一个 prompt(CC bundled loop 的中文忠实改写)。

全文存 _BODY,activate() 经 SkillContribution.prompt_blocks 返回;
由 skill_load 工具按需取,不参与每轮注入。
"""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.skills.contracts import (
    SkillActivation,
    SkillContribution,
    SkillManifest,
    SkillPackage,
    SkillSpec,
)
from xana.skills.interfaces import Skill

_BODY = """# loop — 周期性执行一个 prompt

用法:用户说「每 N 分钟/小时做 X」「循环检查 Y」时使用。

解析规则(按优先级):
1. 开头独立 token 形如 `10m`/`2h`/`1d` → 它是间隔,其余是 prompt
2. 末尾「每 N 分钟/小时/天」或 `every Nm` 子句 → 提取为间隔并从 prompt 删除
3. 都没有 → 默认间隔 10 分钟,整句是 prompt

间隔 → cron 转换:
- N 分钟(N≤59)→ `*/N * * * *`;N≥60 → 取整小时 `0 */H * * *`(H 须整除 24)
- N 小时(N≤23)→ `0 */N * * *`;N 天 → `0 0 */N * *`
- 秒级向上取整到 1 分钟(cron 最小粒度)
- 不整的间隔就近取整,并在调度前告知用户取整结果

执行步骤:
1. 调 cron_create(cron 表达式, prompt, recurring=true)
2. 用人话简短确认排期(频率 + 任务内容)
3. 立即执行一次解析出的 prompt——不等第一次 cron 触发
"""


class LoopSkill(Skill):
    def __init__(self) -> None:
        self._spec = SkillSpec(
            component=ComponentSpec(
                kind="skill",
                name="loop",
                qualified_name="bundled.loop",
                description="按固定间隔循环执行一个 prompt(每 N 分钟/小时做 X)。",
                source=ComponentSource(source_type="builtin", scope="system"),
            ),
            when_to_use="用户要求按间隔循环执行某任务时(每 N 分钟/小时做 X、循环检查 Y)。",
            package=SkillPackage(
                root=None,
                manifest=SkillManifest(name="loop", description="周期性执行一个 prompt"),
                instructions_path=None,
            ),
        )

    def spec(self) -> SkillSpec:
        return self._spec

    async def activate(self, activation: SkillActivation) -> SkillContribution:
        return SkillContribution(prompt_blocks=(_BODY,))

    async def deactivate(self, session_id: str) -> None:
        return None
