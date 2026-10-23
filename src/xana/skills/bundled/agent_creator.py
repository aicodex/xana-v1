"""agent_creator skill — 帮用户创建一个 agent(docs/design/agent-delegation-design.md §3.4)。

流程:访谈 → agent_define catalog 看可选工具 → 起草 → 用户确认 → agent_define create。
xana 暂无交互式审批工具,确认以普通提问完成。
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

_BODY = """# agent_creator — 创建一个 agent

用法:用户想要一个专门做某类事的 agent(例如"帮我建一个只看代码不改代码的审查 agent"),
或希望把某种工作方式固定下来、以后用 /agent 切过去或让别的 agent 委派给它时使用。

第一步:弄清需求(信息不够就逐条问,等回答再继续)
1. 它负责什么?交付什么?(一句话目标 + 产出)
2. 什么时候用它?(会写进 when_to_use,其他 agent 委派时据此挑选)
3. 需要哪些能力?明确不能做什么?(例如"不许改文件""不能联网""只读")

能力分解:需要"执行某个程序"(跑脚本、调命令行工具、处理文件)时,不给 Bash——
先按 tool_creator 技能把那个程序派生成工具(只开放声明过的参数),再把工具名写进 tools。
新 agent 没有 Bash;用户坚持要时,告诉用户由他自己发 /agent allow-exec <name> <原因> 开启,
并说明代价(能执行任意命令,评测时结果可信度降低)。

第二步:看可选工具
调用 agent_define(action="catalog"),查看工具组、每组包含的工具和已有 agent(避免重名)。

第三步:起草
- name:小写英文,可含数字和连字符,2–40 字符,能看出用途(如 code-reviewer)。
- description:一句话说明做什么。
- tools:优先按组选,再零星加减工具;不填 = default(默认组合);[] = 不给任何工具。
  需要排除时用 disallowed_tools(也可写组名),例如只读:tools 选 fs,disallowed_tools 写 Write、Edit。
- system_prompt:写清身份、目标、工作方式、交付格式和禁止事项。
  默认以 ${base_prompt} 开头(保留 xana 默认提示词,在其后追加角色说明);
  只有用户明确要完全自定义时才去掉 ${base_prompt}(此时连环境信息也不会有,需要可写 ${env})。
- 新 agent 的工具不能超出你自己当前可用的工具。

第四步:确认
把 name、description、when_to_use、tools / disallowed_tools、system_prompt 全文展示给用户,
用户明确同意后再调用 agent_define(action="create", ...)。同名 agent 已存在时先问用户是否覆盖。

第五步:告诉用户怎么用
创建成功后告诉用户:/agent <name> 用它开新会话;在其他会话里可以让我用 agent_run 委派给它。
"""


class AgentCreatorSkill(Skill):
    def __init__(self) -> None:
        self._spec = SkillSpec(
            component=ComponentSpec(
                kind="skill",
                name="agent_creator",
                qualified_name="bundled.agent_creator",
                description="创建一个 agent(访谈 → 选工具 → 起草 → 确认 → agent_define 写入)。",
                source=ComponentSource(source_type="builtin", scope="system"),
            ),
            when_to_use="用户想要一个专门做某类事的 agent,或要把某种工作方式固定成可切换、可委派的 agent 时。",
            package=SkillPackage(
                root=None,
                manifest=SkillManifest(name="agent_creator", description="创建 agent"),
                instructions_path=None,
            ),
        )

    def spec(self) -> SkillSpec:
        return self._spec

    async def activate(self, activation: SkillActivation) -> SkillContribution:
        return SkillContribution(prompt_blocks=(_BODY,))

    async def deactivate(self, session_id: str) -> None:
        return None
