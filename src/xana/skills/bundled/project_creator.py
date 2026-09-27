"""project_creator skill — 在聊天里创建一个 xana 项目并接上它的飞书机器人(docs/design/project-design.md §7)。

只在 default 项目(管理台)注册,配合 project_manage 工具。
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

_BODY = """# project_creator — 创建一个 xana 项目

一个项目 = 一套独立的记忆 + 自己的 agent / 技能 / 工具默认配置,由一个独立的 serve 运行,
通常接一个自己的飞书机器人。用户想为某件事(例如投研、某个代码仓库、写作)单独开一个助手时使用。

第一步:弄清需求(信息不够就逐条问,等回答再继续)
1. 这个项目做什么?会在里面聊什么、做哪类工作?
2. 项目目录:默认放在 ~/.xana/projects/<名字>/workspace 即可;用户有现成目录(如代码仓库)时用它的绝对路径。
3. 这个项目里的 agent 需要哪些能力、不能做什么?(例如"只做研究,不许改文件和执行命令")

第二步:拟定配置
- name:小写英文,可含数字、-、_,能看出用途(如 invest、blog-writing)。
- agent_defaults(可选,不需要限制就不填):
  - tools:工具选择,默认 ["default"];可写组名(fs、exec、web、memory、session、meta、research、
    schedule、agents)或工具名;
  - disallowed_tools:在 tools 之上再排除,例如只读研究:tools 用 ["default"],disallowed_tools 写
    ["exec", "Write", "Edit"];
  - skills:可见技能,不填 = 全部,[] = 没有。
  这是项目里 agent 的默认配置:以后在这个项目里新建的 agent 不写工具 / 技能时就用它。
- 把 name、目录、agent_defaults 给用户确认。

第三步:创建并接机器人
1. project_manage(action="create", ...);
2. project_manage(action="bind_feishu", name=...),把返回的授权链接原样发给用户:
   "在飞书里点开这个链接,确认创建机器人";然后结束这一轮,等用户回复;
3. 用户说已确认后,project_manage(action="feishu_status", name=...);
4. project_manage(action="start", name=...)。

第四步:告诉用户
新机器人的名字、启动约需一分钟、之后直接去和它聊天;用户已自动成为它的授权用户。
需要调整默认配置时用 configure,改完 stop 再 start。
"""


class ProjectCreatorSkill(Skill):
    def __init__(self) -> None:
        self._spec = SkillSpec(
            component=ComponentSpec(
                kind="skill",
                name="project_creator",
                qualified_name="bundled.project_creator",
                description="创建一个 xana 项目并接上它的飞书机器人(访谈 → 配置 → 创建 → 扫码接机器人 → 启动)。",
                source=ComponentSource(source_type="builtin", scope="system"),
            ),
            when_to_use="用户想为某件事单独开一个助手 / 项目 / 飞书机器人时。",
            package=SkillPackage(
                root=None,
                manifest=SkillManifest(name="project_creator", description="创建 xana 项目"),
                instructions_path=None,
            ),
        )

    def spec(self) -> SkillSpec:
        return self._spec

    async def activate(self, activation: SkillActivation) -> SkillContribution:
        return SkillContribution(prompt_blocks=(_BODY,))

    async def deactivate(self, session_id: str) -> None:
        return None
