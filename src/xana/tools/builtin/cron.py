"""cron_create / cron_list / cron_delete — 定时任务三工具(deferred)。

构造注入 SchedulerService(ABC,extension_api.capabilities)。三个类同属
一个能力,变更同源,放一个文件。spec 全部 defer=True:模型默认只见名字,
经 tool_search 揭示后才能调用。

session 归属:SchedulerService ABC 的 add() 不接收 session_id,而任务必须
记录创建它的会话(到点后结果送回该聊天当前的会话,docs/design/scheduled-task-design.md)。
FileSchedulerService 暴露公有可变属性 session_id,CronCreateTool.execute 在
add 之前用 ToolContext.session_id 覆写——工具执行在同一事件循环内串行,无并发风险。因此 CronCreateTool 标注具体
类型 FileSchedulerService;list/delete 只用 ABC 方法,标注 ABC。
"""
from __future__ import annotations

from collections.abc import Mapping

from xana.agents.interfaces import Agent
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.extension_api.capabilities import SchedulerService
from xana.policy.contracts import PermissionResult
from xana.runtime.scheduler import FileSchedulerService
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_CREATE_SPEC = ToolSpec(
    toolset="schedule",
    component=ComponentSpec(
        kind="tool", name="cron_create", qualified_name="builtin.cron_create",
        description=(
            "调度一个 prompt 在未来运行——按 cron 表达式循环触发,或在指定时间触发一次。"
            "任务随当前项目持久化,serve 重启后仍然有效。触发时在独立的新会话里执行(看不到当前对话,"
            "prompt 要写得能独立执行),无人值守;按 notify 决定是否把结果送回当前聊天。\n"
            "cron 语法为本地时区的标准 5 字段:\"分 时 日 月 周\"。"
            "一次性任务(recurring=false)把分/时/日/月固定为具体值,触发一次后自动删除。"
            "循环任务示例:\"*/5 * * * *\" 每 5 分钟,\"0 * * * *\" 每小时,\"0 9 * * 1-5\" 工作日 9 点。"
            "避免 :00 和 :30 整点——选错峰分钟(如 :57、:03、:07)。"),
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "cron": {"type": "string",
                     "description": '本地时区标准 5 字段 cron 表达式:"分 时 日 月 周"'
                                    '(如 "*/5 * * * *" = 每 5 分钟)。'},
            "prompt": {"type": "string", "description": "每次触发时执行的任务,要写得不依赖当前对话就能完成。"},
            "recurring": {"type": "boolean",
                          "description": "true(默认)= 每次匹配都触发直至删除;"
                                         "false = 下次匹配触发一次后自动删除。"},
            "agent": {"type": "string",
                      "description": "到点运行的 agent 名(专门的 agent / 工作流);不填 = 当前会话的 agent。"},
            "notify": {"type": "string", "enum": ["report", "silent"],
                       "description": "report(默认)= 每次跑完把结果发给用户;silent = 后台静默,"
                                      "只在失败、需要用户决策或任务主动通知时才发消息。"},
        },
        "required": ["cron", "prompt"],
    },
    defer=True,
    display=ToolDisplay(category="other", verb="创建定时任务", summary_args=("cron", "prompt")),
)

_LIST_SPEC = ToolSpec(
    toolset="schedule",
    component=ComponentSpec(
        kind="tool", name="cron_list", qualified_name="builtin.cron_list",
        description="列出 cron_create 调度的全部定时任务(含持久化的)。",
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={"type": "object", "properties": {}, "required": []},
    read_only=True,
    defer=True,
    display=ToolDisplay(category="other", verb="查看定时任务", result="none"),
)

_DELETE_SPEC = ToolSpec(
    toolset="schedule",
    component=ComponentSpec(
        kind="tool", name="cron_delete", qualified_name="builtin.cron_delete",
        description="按 ID 取消 cron_create 调度的定时任务。",
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "cron_create 返回的任务 ID。"},
        },
        "required": ["id"],
    },
    defer=True,
    display=ToolDisplay(category="other", verb="删除定时任务", summary_args=("id",)),
)


class _CronToolBase(Tool):

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="本地定时任务管理")


class CronCreateTool(_CronToolBase):
    def __init__(self, scheduler: FileSchedulerService, agents: Mapping[str, Agent] | None = None) -> None:
        """agents:活的 agent 表,用于校验 agent 参数(None = 不校验)。"""
        self.scheduler = scheduler
        self._agents = agents

    def spec(self) -> ToolSpec:
        return _CREATE_SPEC

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        cron = str(invocation.input.get("cron", ""))
        prompt = str(invocation.input.get("prompt", ""))
        recurring = bool(invocation.input.get("recurring", True))
        agent = str(invocation.input.get("agent") or "").strip() or None
        notify = str(invocation.input.get("notify") or "report")
        if agent is not None and self._agents is not None and agent not in self._agents:
            return ToolResult(ok=False, content=None, failure_kind="validation", error=f"未知 agent {agent!r}",
                              rendered=f"[cron_create 失败: 未知 agent {agent!r},可选:{', '.join(sorted(self._agents))}]")
        # 任务记录的 session_id = 创建它的会话(runner 串行消费,赋值无并发风险)
        if context.session_id:
            self.scheduler.session_id = context.session_id
        try:
            task_id = await self.scheduler.add(cron=cron, prompt=prompt, recurring=recurring,
                                               agent=agent, notify=notify)
        except ValueError as e:
            return ToolResult(ok=False, content=None, failure_kind="validation",
                              error=str(e), rendered=f"[cron_create 失败: {e}]")
        return ToolResult(
            ok=True,
            content={"id": task_id, "cron": cron, "recurring": recurring, "agent": agent, "notify": notify},
            rendered=(f"[cron_create] 已调度 {task_id}: {cron} → {prompt[:50]}"
                      f"(agent: {agent or '当前会话的 agent'},{'静默' if notify == 'silent' else '每次报告结果'})"),
        )


class CronListTool(_CronToolBase):
    def __init__(self, scheduler: SchedulerService) -> None:
        self.scheduler = scheduler

    def spec(self) -> ToolSpec:
        return _LIST_SPEC

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        jobs = await self.scheduler.list_all()
        shaped = [{"id": j["id"], "cron": j["cron"], "prompt": j["prompt"], "recurring": j["recurring"],
                   "agent": j.get("agent"), "notify": j.get("notify", "report"), "last_run": j.get("last_run")}
                  for j in jobs]
        rendered = "\n".join(_render_job(j) for j in shaped) or "(无定时任务)"
        return ToolResult(ok=True, content={"jobs": shaped}, rendered=rendered)


class CronDeleteTool(_CronToolBase):
    def __init__(self, scheduler: SchedulerService) -> None:
        self.scheduler = scheduler

    def spec(self) -> ToolSpec:
        return _DELETE_SPEC

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        task_id = str(invocation.input.get("id", ""))
        await self.scheduler.remove(task_id)
        return ToolResult(ok=True, content={"id": task_id},
                          rendered=f"[cron_delete] 已删除 {task_id}")


def _render_job(j: dict) -> str:
    last = j["last_run"]
    tail = (f";最近一次 {last['at']} {last['status']},记录 {last['transcript']}" if last else ";还没执行过")
    return (f"- {j['id']} {j['cron']} recurring={j['recurring']} agent={j['agent'] or '当前会话的 agent'} "
            f"notify={j['notify']}: {j['prompt'][:60]}{tail}")
