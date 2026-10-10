"""schedule skill — 管理本地定时任务(CC scheduleRemoteAgents 的本地适配版)。

保留 CC 的交互流程(先弄清意图 → 定时间 → 确认 → 执行),执行端换成
cron_create/cron_list/cron_delete 三个本地工具;remote 形态记 todo md。
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

_BODY = """# schedule — 管理定时任务(本地)

用法:用户要「定时/每天/提醒我/排个任务」时使用。任务由本机 serve 进程的调度器
执行,持久化在当前项目的 scheduled_tasks.json,serve 重启后仍有效。到点后任务在独立的
新会话里执行,完成后结果作为通知送回当前聊天(用户 /clear 之后送到新会话),用户可以接着追问。

第一步先弄清用户要做什么:创建 / 查看 / 修改 / 删除。

创建流程:
1. 理解目标:做什么、成功标准是什么。prompt 要写得具体、能独立执行——
   触发时模型只看到这段 prompt,没有当前对话上下文(把需要的股票代码、路径、口径都写进去)。
2. 定时间:问清何时/多频繁,换算成本地时区的 5 字段 cron 表达式,
   把换算结果用人话复述给用户确认(如「每个工作日 9:03」)。
   避开 :00/:30 整点,选错峰分钟。
3. 一次性任务用 recurring=false(触发一次后自动删除);周期任务 recurring=true。
4. 由谁来跑:要跑某个专门的 agent / 工作流时填 agent(须是已有的 agent);不填 = 当前会话的 agent。
5. 怎么通知:每次都要看结果(日报、汇总)用 notify="report"(默认);只要做完、不用每次看
   (数据更新、定期整理、监控)用 notify="silent"——失败、需要用户拍板或发现要紧的事时仍会通知。
   任务在无人值守模式下运行,需要用户决定时会用 notify_user 发问,用户答复后接着做。
6. 用户确认后调 cron_create,把返回的任务 ID 告诉用户。

查看:cron_list 列出全部任务(ID/cron/agent/通知方式/prompt/最近一次执行的时间、状态和记录路径)。
修改:先 cron_delete 旧任务再 cron_create 新任务(无原地更新)。
删除:cron_delete(id)。删除前把要删的任务复述给用户确认。
"""


class ScheduleSkill(Skill):
    def __init__(self) -> None:
        self._spec = SkillSpec(
            component=ComponentSpec(
                kind="skill",
                name="schedule",
                qualified_name="bundled.schedule",
                description="创建/查看/修改/删除本地定时任务(cron,serve 重启后仍有效)。",
                source=ComponentSource(source_type="builtin", scope="system"),
            ),
            when_to_use="用户要定时执行、每天提醒、排个任务,或管理已有定时任务时。",
            package=SkillPackage(
                root=None,
                manifest=SkillManifest(name="schedule", description="管理本地定时任务"),
                instructions_path=None,
            ),
        )

    def spec(self) -> SkillSpec:
        return self._spec

    async def activate(self, activation: SkillActivation) -> SkillContribution:
        return SkillContribution(prompt_blocks=(_BODY,))

    async def deactivate(self, session_id: str) -> None:
        return None
