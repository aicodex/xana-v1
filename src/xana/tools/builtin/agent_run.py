"""agent_run:把子任务委派给另一个 agent,同步取回它的最终回答(docs/design/agent-delegation-design.md §3.2)。

子 agent 在独立子会话里跑完整的 harness run(空上下文);工具 = 它自己的工具选择 ∩ 上限(调用方可用工具
− 委派 / 创建 agent / 会话控制类工具),因此子 agent 不能再委派。可用 resume 在同一子会话里继续。
"""
from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping

from xana.agents.delegation import AGENT_RUN_TOOL, subagent_ceiling
from xana.agents.interfaces import Agent, AgentRegistry
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.extension_api.capabilities import AgentRunner
from xana.extension_api.contracts import SubagentStep
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
    "- 互不依赖的多个子任务,在同一条消息里发出多个 agent_run 调用,它们会并行执行。\n"
    "- 需要让同一个子 agent 接着做时,用 resume 传入它上次返回的 agent_id,它会保留之前的上下文。\n"
    "- 子 agent 的回答是它对自己工作的描述,关键结论需要时自行核实;不要编造尚未返回的结果。"
)

_SPEC = ToolSpec(
    toolset="agents",
    component=ComponentSpec(
        kind="tool", name=AGENT_RUN_TOOL, qualified_name="builtin.agent_run", description=_DESCRIPTION,
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "3–8 个字的任务标题,用于显示进度"},
            "prompt": {"type": "string", "description": "给子 agent 的完整任务说明"},
            "subagent_type": {"type": "string", "description": "要委派的 agent 名;默认 chat。与 resume 二选一"},
            "resume": {"type": "string", "description": "要继续的子 agent 的 agent_id(上次 agent_run 返回);与 subagent_type 二选一"},
        },
        "required": ["description", "prompt"],
    },
    open_world=True, concurrency="parallel",
    display=ToolDisplay(category="delegate", verb="委派", summary_args=("description",)),
)


class AgentRunTool(Tool):
    def __init__(self, *, runner: AgentRunner, agents: Mapping[str, Agent], tool_registry: ToolRegistry,
                 agent_registry: AgentRegistry | None = None,
                 default_agent: str = "chat", max_concurrent: int = MAX_CONCURRENT) -> None:
        self._runner = runner
        self._agents = agents            # 活的映射:增删改的 agent 立即反映
        self._agent_registry = agent_registry
        self._tools = tool_registry
        self._default_agent = default_agent
        self._max_concurrent = max_concurrent
        self._running = 0

    def spec(self) -> ToolSpec:
        return _SPEC

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
        """agent_id 须是本会话派出的子会话;返回其 agent 名。"""
        m = re.fullmatch(re.escape(f"{parent}-agent-") + r"(.+)-[0-9a-f]{6}", agent_id)
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
        if self._running >= self._max_concurrent:
            return _fail(f"同时运行的子 agent 已达上限 {self._max_concurrent} 个。不要重试,直接用自己的工具完成",
                         retryable=False)
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

        self._running += 1
        t0 = time.monotonic()
        try:
            progress(f"{name} 开始")
            activity(ToolActivity(lane="sub", lane_title=lane_title, kind="note", text="开始"))
            outcome = await self._runner.run(
                agent_name=name, prompt=prompt, session_id=child, parent_run_id=context.run_id,
                parent_session_id=context.session_id, tool_ceiling=ceiling, on_step=on_step)
        finally:
            self._running -= 1
        activity(ToolActivity(lane="sub", lane_title=lane_title,
                              kind="done" if outcome.status == "completed" else "error",
                              text=f"{'完成' if outcome.status == 'completed' else '未完成'} · 工具 {calls[0]} 次"))
        elapsed = time.monotonic() - t0
        tools = sum(outcome.tool_counts.values())
        head = (f"[agent_run] {name} · agent_id={child} · 状态 {outcome.status} · 用时 {elapsed:.0f} 秒 · "
                f"调用工具 {tools} 次")
        tail = f"\n\n(需要让这个子 agent 继续时,用 resume=\"{child}\")"
        if outcome.status != "completed":
            return ToolResult(ok=False, content={"agent_id": child, "status": outcome.status},
                              failure_kind="execution", error=outcome.error or outcome.status,
                              rendered=f"{head}\n失败:{outcome.error or outcome.status}{tail}")
        return ToolResult(
            ok=True, content={"agent_id": child, "agent": name, "status": outcome.status, "tool_calls": tools},
            rendered=f"{head}\n\n{outcome.text}{tail}",
            metadata={"display_text": f"{name} · 工具 {tools} 次 · {elapsed:.0f} 秒"})


def _fail(message: str, *, retryable: bool = True) -> ToolResult:
    return ToolResult(ok=False, content=None, failure_kind="validation", error=message,
                      rendered=f"[agent_run] {message}", retryable=retryable)
