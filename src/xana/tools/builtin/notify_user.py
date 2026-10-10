"""notify_user:定时任务(无人值守)在关键时刻通知用户或请用户决策(docs/design/scheduled-task-design.md §4.3)。

只在无人值守会话中可见(harness 按 PromptProfile.mode 过滤)。描述照 Claude Code PushNotification 的克制写法;
"告知不能拿来提问"照 Kimi Code notify-user。送达与登记由 ScheduledTaskDispatcher 负责。
"""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.harness_sdk.contracts import NOTIFY_USER_TOOL
from xana.policy.contracts import PermissionResult
from xana.runtime.scheduler import ScheduledTaskDispatcher
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

_DESCRIPTION = (
    "定时任务运行时通知用户(没人在实时看)。用户不需要的通知会越积越烦,宁可不发:日常进度、按计划完成的"
    "小步骤都不要通知;只在发现用户需要知道的实质变化,或必须由用户拍板时使用。\n"
    "- kind=\"info\":告知一件事,立即送到用户的聊天,任务继续。不要用它提问或要决定。每次任务最多 3 条。\n"
    "- kind=\"decision\":必须由用户决定才能继续时使用,写清问题,options 列出可选项。调用后结束本次执行:"
    "最后一条消息写清做到了哪里、在等什么决定;用户答复后会在本会话里接着做。"
)

_SPEC = ToolSpec(
    toolset="schedule",
    component=ComponentSpec(
        kind="tool", name=NOTIFY_USER_TOOL, qualified_name=f"builtin.{NOTIFY_USER_TOOL}", description=_DESCRIPTION,
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["info", "decision"]},
            "message": {"type": "string", "description": "info:通知内容;decision:要用户决定的问题(写清背景)"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "decision:可选项"},
        },
        "required": ["kind", "message"],
    },
    open_world=True,
    display=ToolDisplay(category="other", verb="通知用户", summary_args=("message",)),
)


class NotifyUserTool(Tool):
    def __init__(self, *, dispatcher: ScheduledTaskDispatcher) -> None:
        self._dispatcher = dispatcher

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not str(invocation.input.get("message") or "").strip():
            raise ValueError("message 不能为空")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="定时任务通知用户")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        message = str(invocation.input["message"]).strip()
        if invocation.input.get("kind") == "decision":
            options = [str(o) for o in invocation.input.get("options") or []]
            if self._dispatcher.request_decision(context.session_id, message, options):
                return _ok("[notify_user] 问题已记下,本次执行结束后会连同进展一起发给用户。现在结束本次执行:"
                           "最后一条消息写清做到了哪里、在等什么决定;不要再继续做依赖这个决定的工作。")
            return _ok("[notify_user] 现在结束本次执行,在最后一条消息里写清问题、选项和目前的进展,"
                       "由调用方转告用户。")
        error = await self._dispatcher.notify(context.session_id, message)
        if error is not None:
            return ToolResult(ok=False, content=None, error=error, rendered=f"[notify_user 失败: {error}]")
        return _ok("[notify_user] 已送达用户,继续任务。")


def _ok(text: str) -> ToolResult:
    return ToolResult(ok=True, content=None, rendered=text)
