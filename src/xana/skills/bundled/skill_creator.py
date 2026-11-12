"""skill_creator skill — 把本次会话的可复用流程沉淀为 skill(CC skillify 的中文忠实版)。

访谈降级为普通提问——xana 暂无 request_user_input 工具(见 todo_ai/deferred_features.md)。
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

_BODY = """# skill_creator — 把本次会话的可复用流程沉淀为 skill

用法:用户要安装外部技能，或说「把刚才的流程存成 skill」「以后还要这么做」时使用。

安装外部技能（用户要求安装现成技能时使用，无需走下方创作访谈）：
使用 skill_manage 工具，不用 Bash 直接安装后宣称所有 agent 都可用。
1. action=list：先查看本地库存和已启用的 agent；指定 source 可查看来源中的技能。
2. action=install, skills=[技能名]：先查本地，已有直接复用，缺少时才通过官方 npx 安装。
   source 填仓库或官方 CLI 支持的来源；skills=["*"] 表示明确安装全部。
3. 安装默认不启用。agents=[目标 agent 名] 明确指定时，安装就绪后仅为这些 agent 注册。
   如果用户只要求安装而没指定用途或 agent，先保持未启用，不擅自全局开放。
4. action=enable/disable, skills=[技能名], agents=[目标 agent 名]：按 agent 调整可用范围。
5. 从目标 agent 调用 skill_load 验证；加载返回技能目录，按该目录读取 references/。
安装不执行包内脚本，也不会自动提供第三方工具、API 或凭据。

以下用于创作新技能：

第一步:分析会话
回顾本次会话:用户的目标是什么?执行了哪些步骤?哪些步骤每次都一样,
哪些会随参数变化?

第二步:访谈用户(逐轮提问,等回答再继续)
1. 名字与目标:建议一个 kebab-case 名字和一句话目标,请用户确认或修改
2. 步骤与参数:列出你识别的步骤,问哪些值应做成参数($arg 形式)
3. 每步细节:成功标准是什么?哪步要停下来等人确认?哪些步骤可并行?
4. 触发语:用户将来会怎么说来触发这个 skill?有什么坑要写进注意事项?

第三步:写 SKILL.md
格式(YAML frontmatter + 正文):
---
name: {skill-name}
description: {一句话描述}
when_to_use: {触发语和例子}
arguments: {参数名列表}
---
# {标题}
## 输入
- `$参数名`: 说明
## 目标
明确陈述目标与产出(成功标准)
## 步骤
### 1. 步骤名
做什么,要具体。
**成功标准**: 必填
(可选注记:产物 / 人工确认点 / 硬性规则;3a/3b 编号表示可并行)

第四步:把 SKILL.md 全文展示给用户确认,确认后写入用户指定的位置
(默认 ~/.xana/skills/{name}/SKILL.md)。
"""


class SkillCreatorSkill(Skill):
    def __init__(self) -> None:
        self._spec = SkillSpec(
            component=ComponentSpec(
                kind="skill",
                name="skill_creator",
                qualified_name="bundled.skill_creator",
                description="安装并按 agent 启用外部技能（skill_manage），或把可复用流程沉淀为 SKILL.md。",
                source=ComponentSource(source_type="builtin", scope="system"),
            ),
            when_to_use="用户要安装、列出外部技能，或把刚才的流程存成 skill 时。",
            package=SkillPackage(
                root=None,
                manifest=SkillManifest(name="skill_creator", description="沉淀可复用流程为 skill"),
                instructions_path=None,
            ),
        )

    def spec(self) -> SkillSpec:
        return self._spec

    async def activate(self, activation: SkillActivation) -> SkillContribution:
        return SkillContribution(prompt_blocks=(_BODY,))

    async def deactivate(self, session_id: str) -> None:
        return None
