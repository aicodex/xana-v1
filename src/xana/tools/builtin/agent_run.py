"""agent_run:把子任务委派给另一个 agent,同步取回它的最终回答(docs/design/agent-delegation-design.md §3.2)。

子 agent 在独立子会话里跑完整的 harness run(空上下文);工具 = 它自己的工具选择 ∩ 上限(调用方可用工具
− 委派 / 创建 agent / 会话控制类工具),因此子 agent 不能再委派。可用 resume 在同一子会话里继续。
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Mapping

from xana.agents.delegation import AGENT_RUN_TOOL, subagent_ceiling
from xana.agents.interfaces import Agent, AgentRegistry
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.extension_api.capabilities import AgentRunner
from xana.extension_api.contracts import SubagentStep
from xana.harness_sdk.background import BackgroundTasks, TaskOutcome
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolActivity, ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool, ToolRegistry

MAX_CONCURRENT = 6

_DESCRIPTION = (
    "把一个子任务委派给另一个 agent 独立完成,等它结束后返回它的最终回答。可委派的 agent 及其工具见对话中的"
    "「可委派的 agent」列表。\n\n"
    "适合:可以独立完成、会产生大量中间过程的子任务(如大范围检索、逐个排查多个文件),或需要另一个 agent 的专长。"
    "目标文件已经明确、几步就能完成的事自己做,不要委派。\n\n"
    "写任务说明(prompt)要像给刚走进房间的同事交代工作:子 agent 看不到当前对话,说明目标、已知信息、"
    "需要交付什么、范围边界。不要把「理解问题」也委派出去——先自己弄清楚要什么,再写具体的任务。\n\n"
    "- 子 agent 的回答用户看不到,需要的内容要由你转述给用户。\n"
    "- 互不依赖的多个子任务,在同一条消息里发出多个 agent_run 调用,它们会并行执行;同时最多运行 {max_concurrent} 个"
    "子 agent,超出的排队等前面的完成后再开始,所以子任务多时按重要性排好先后,一次发出不超过 {max_concurrent} 个最省时间。\n"
    "- 需要让同一个子 agent 接着做时,用 resume 传入它上次返回的 agent_id,它会保留之前的上下文。\n"
    "- 子 agent 的回答是它对自己工作的描述,关键结论需要时自行核实;不要编造尚未返回的结果。\n"
    "- 先等待 wait_s 秒(默认 {wait_default},0 = 不等直接转后台):期间完成就直接返回回答;没完成就返回后台任务 id,"
    "子 agent 继续运行——用 task_wait 等它,或先做别的事,它结束时结果会自动送到这里。"
)

WAIT_DEFAULT_S = 600
WAIT_MAX_S = 1800


def _spec(max_concurrent: int) -> ToolSpec:
    """描述里的并发上限取自实际参数(工具描述中的限制都从行为参数生成,不手写数字)。"""
    return ToolSpec(
        temporal="as_of",
        toolset="agents",
        component=ComponentSpec(
            kind="tool", name=AGENT_RUN_TOOL, qualified_name="builtin.agent_run",
            description=_DESCRIPTION.format(max_concurrent=max_concurrent, wait_default=WAIT_DEFAULT_S),
            source=ComponentSource(source_type="builtin", scope="system")),
        input_schema={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "3–8 个字的任务标题,用于显示进度"},
                "prompt": {"type": "string", "description": "给子 agent 的完整任务说明"},
                "subagent_type": {"type": "string", "description": "要委派的 agent 名;默认 chat。与 resume 二选一"},
                "resume": {"type": "string", "description": "要继续的子 agent 的 agent_id(上次 agent_run 返回);与 subagent_type 二选一"},
                "wait_s": {"type": "integer", "minimum": 0, "maximum": WAIT_MAX_S,
                           "description": f"最多等待多少秒再转后台,默认 {WAIT_DEFAULT_S}"},
            },
            "required": ["description", "prompt"],
        },
        open_world=True, concurrency="parallel",
        display=ToolDisplay(category="delegate", verb="委派", summary_args=("description",)),
    )


class AgentRunTool(Tool):
    def __init__(self, *, runner: AgentRunner, agents: Mapping[str, Agent], tool_registry: ToolRegistry,
                 agent_registry: AgentRegistry | None = None,
                 default_agent: str = "chat", max_concurrent: int = MAX_CONCURRENT,
                 background: BackgroundTasks | None = None) -> None:
        """background:超时转后台(None = 一直阻塞到子 agent 结束)。"""
        self._runner = runner
        self._background = background
        self._agents = agents            # 活的映射:增删改的 agent 立即反映
        self._agent_registry = agent_registry
        self._tools = tool_registry
        self._default_agent = default_agent
        self._max_concurrent = max_concurrent
        # 同时运行的子 agent 上限:超出的排队等空位(不拒绝,模型一次并行派出多个时也都能跑完)
        self._slots = asyncio.Semaphore(max_concurrent)
        self._spec = _spec(max_concurrent)

    def spec(self) -> ToolSpec:
        return self._spec

    async def validate(self, invocation: ToolInvocation) -> None:
        if not str(invocation.input.get("prompt") or "").strip():
            raise ValueError("prompt 不能为空")
        if invocation.input.get("resume") and invocation.input.get("subagent_type"):
            raise ValueError("resume 与 subagent_type 只能二选一")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        # 子 agent 的每个工具调用仍各自走权限检查;这里只负责派发
        return PermissionResult(decision="allow", reason="委派子 agent")

    def _child_session(self, parent: str, agent: str) -> str:
        return f"{parent}-agent-{agent}-{uuid.uuid4().hex[:6]}"

    def _resume_target(self, parent: str, agent_id: str) -> str | None:
        """agent_id 须是本会话派出的子会话(agent_run 委派的,或定时任务的子会话
        {会话}-cron-{任务}-{agent}-{随机},docs/design/scheduled-task-design.md §4.3);返回其 agent 名。"""
        m = (re.fullmatch(re.escape(f"{parent}-agent-") + r"(.+)-[0-9a-f]{6}", agent_id)
             or re.fullmatch(re.escape(f"{parent}-cron-") + r"[0-9a-f]{8}-(.+)-[0-9a-f]{6}", agent_id))
        return m.group(1) if m else None

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        prompt = str(invocation.input["prompt"]).strip()
        resume = str(invocation.input.get("resume") or "").strip()
        if resume:
            name = self._resume_target(context.session_id, resume)
            if name is None:
                return _fail(f"agent_id {resume!r} 不是本会话派出的子 agent")
            child = resume
        else:
            name = str(invocation.input.get("subagent_type") or self._default_agent).strip()
            child = self._child_session(context.session_id, name)
        if self._agent_registry is not None:
            await self._agent_registry.refresh()
        if name not in self._agents:
            return _fail(f"没有名为 {name!r} 的 agent,可委派的 agent 见「可委派的 agent」列表")
        ceiling = subagent_ceiling(context.available_tools, await self._tools.list_specs(),
                                   await self._tools.list_toolsets())
        progress = context.report_progress or (lambda _text: None)
        activity = context.report_activity or (lambda _a: None)
        lane_title = f"{name}:{str(invocation.input.get('description') or '').strip() or '子任务'}"
        calls = [0]

        def on_step(step: SubagentStep) -> None:
            if step.kind == "thinking":
                activity(ToolActivity(lane="sub", lane_title=lane_title, kind="thinking", text=step.text))
            elif step.kind == "tool":
                calls[0] += 1
                progress(f"{name} 运行中 · 已调用工具 {calls[0]} 次")
                kind = {"web_search": "search", "web_open": "open"}.get(step.category, "tool")
                label = {"search": "搜索", "open": "打开"}.get(kind, step.tool_name)
                activity(ToolActivity(lane="sub", lane_title=lane_title, kind=kind,
                                      text=f"{label}:{step.text}" if step.text else label))

        async def run_child(bg_progress=None) -> TaskOutcome:
            if self._slots.locked():
                progress(f"{name} 排队中:同时最多 {self._max_concurrent} 个子 agent,等前面的完成")
                activity(ToolActivity(lane="sub", lane_title=lane_title, kind="note", text="排队等待空位"))
            async with self._slots:
                t0 = time.monotonic()
                progress(f"{name} 开始")
                activity(ToolActivity(lane="sub", lane_title=lane_title, kind="note", text="开始"))

                def step(s: SubagentStep) -> None:
                    on_step(s)
                    if bg_progress is not None and s.kind == "tool":
                        bg_progress(f"工具 {s.tool_name}:{s.text}" if s.text else f"工具 {s.tool_name}")

                outcome = await self._runner.run(
                    agent_name=name, prompt=prompt, session_id=child, parent_run_id=context.run_id,
                    parent_session_id=context.session_id, tool_ceiling=ceiling, on_step=step,
                    tool_env=context.env)    # 评测环境(时点 / 回放)随委派下传
            activity(ToolActivity(lane="sub", lane_title=lane_title,
                                  kind="done" if outcome.status == "completed" else "error",
                                  text=f"{'完成' if outcome.status == 'completed' else '未完成'} · 工具 {calls[0]} 次"))
            elapsed = time.monotonic() - t0
            tools = sum(outcome.tool_counts.values())
            head = (f"[agent_run] {name} · agent_id={child} · 状态 {outcome.status} · 用时 {elapsed:.0f} 秒 · "
                    f"调用工具 {tools} 次")
            tail = f"\n\n(需要让这个子 agent 继续时,用 resume=\"{child}\")"
            if outcome.status != "completed":
                return TaskOutcome(ok=False, rendered=f"{head}\n失败:{outcome.error or outcome.status}{tail}",
                                   content={"agent_id": child, "status": outcome.status,
                                            "error": outcome.error or outcome.status})
            return TaskOutcome(ok=True, rendered=f"{head}\n\n{outcome.text}{tail}",
                               content={"agent_id": child, "agent": name, "status": outcome.status,
                                        "tool_calls": tools, "elapsed": elapsed})

        if self._background is None:
            return _to_result(await run_child())
        wait_s = min(int(invocation.input.get("wait_s", WAIT_DEFAULT_S)), WAIT_MAX_S)
        rec = self._background.start(kind="agent", label=f"{name}:{lane_title.split(':', 1)[1]}",
                                     parent_session=context.session_id, parent_run=context.run_id or "",
                                     run=run_child)
        try:
            rec = await self._background.wait(rec.id, wait_s)
        except asyncio.CancelledError:
            self._background.stop(rec.id)      # 父 run 在等待期间被取消:子 agent 一起停
            raise
        if rec.status == "running":
            return ToolResult(ok=True, content={"task_id": rec.id, "agent_id": child, "status": "running"},
                              rendered=(f"[agent_run] {name} 已运行 {wait_s} 秒仍未结束,转为后台任务 {rec.id}"
                                        f"(agent_id={child})。最近进度:{'; '.join(rec.progress[-3:]) or '无'}。\n"
                                        f"用 task_wait(task_id=\"{rec.id}\") 等它,或先做别的事;它结束时结果会自动送到这里。"),
                              metadata={"display_text": f"{name} · 转后台 {rec.id}"})
        return _to_result(TaskOutcome(ok=rec.status == "completed", rendered=rec.rendered, content=rec.content))


def _to_result(out: TaskOutcome) -> ToolResult:
    if not out.ok:
        return ToolResult(ok=False, content=out.content, failure_kind="execution",
                          error=str(out.content.get("error") or "子 agent 未完成"), rendered=out.rendered)
    return ToolResult(ok=True, content=out.content, rendered=out.rendered, metadata={
        "display_text": f"{out.content.get('agent')} · 工具 {out.content.get('tool_calls')} 次 · "
                        f"{out.content.get('elapsed', 0):.0f} 秒"})


def _fail(message: str, *, retryable: bool = True) -> ToolResult:
    return ToolResult(ok=False, content=None, failure_kind="validation", error=message,
                      rendered=f"[agent_run] {message}", retryable=retryable)
