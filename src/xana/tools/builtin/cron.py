"""cron_create / cron_list / cron_delete — 定时任务三工具(deferred)。

构造注入 SchedulerService(ABC,extension_api.capabilities)。三个类同属
一个能力,变更同源,放一个文件。spec 全部 defer=True:模型默认只见名字,
经 tool_search 揭示后才能调用。

session 归属:SchedulerService ABC 的 add() 不接收 session_id,而任务必须
记录创建它的会话(runner 到点把 prompt 注回该 session)。裁定(计划 Task 4):
FileSchedulerService 暴露公有可变属性 session_id,CronCreateTool.execute 在
add 之前用 ToolContext.session_id 覆写——SchedulerRunner 串行消费任务,
工具执行也在同一事件循环内串行,无并发风险。因此 CronCreateTool 标注具体
类型 FileSchedulerService;list/delete 只用 ABC 方法,标注 ABC。
"""
from __future__ import annotations

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
            "任务持久化到 ~/.xana/scheduled_tasks.json,serve 重启后仍然有效。\n"
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
            "prompt": {"type": "string", "description": "每次触发时注入的 prompt。"},
            "recurring": {"type": "boolean",
                          "description": "true(默认)= 每次匹配都触发直至删除;"
                                         "false = 下次匹配触发一次后自动删除。"},
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
    async def validate(self, invocation: ToolInvocation) -> None:
        return None

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="本地定时任务管理")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None


class CronCreateTool(_CronToolBase):
    def __init__(self, scheduler: FileSchedulerService) -> None:
        self.scheduler = scheduler

    def spec(self) -> ToolSpec:
        return _CREATE_SPEC

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        cron = str(invocation.input.get("cron", ""))
        prompt = str(invocation.input.get("prompt", ""))
        recurring = bool(invocation.input.get("recurring", True))
        # 任务记录的 session_id = 创建它的会话(runner 串行消费,赋值无并发风险)
        if context.session_id:
            self.scheduler.session_id = context.session_id
        try:
            task_id = await self.scheduler.add(cron=cron, prompt=prompt, recurring=recurring)
        except ValueError as e:
            return ToolResult(ok=False, content=None, failure_kind="validation",
                              error=str(e), rendered=f"[cron_create 失败: {e}]")
        return ToolResult(
            ok=True,
            content={"id": task_id, "cron": cron, "recurring": recurring},
            rendered=f"[cron_create] 已调度 {task_id}: {cron} → {prompt[:50]}",
        )


class CronListTool(_CronToolBase):
    def __init__(self, scheduler: SchedulerService) -> None:
        self.scheduler = scheduler

    def spec(self) -> ToolSpec:
        return _LIST_SPEC

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        jobs = await self.scheduler.list_all()
        shaped = [{"id": j["id"], "cron": j["cron"], "prompt": j["prompt"],
                   "recurring": j["recurring"]} for j in jobs]
        rendered = "\n".join(
            f"- {j['id']} {j['cron']} recurring={j['recurring']}: {j['prompt'][:60]}"
            for j in shaped) or "(无定时任务)"
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
