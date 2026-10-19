"""deep_research:主模型当研究负责人,拆子课题 → 并行派出子研究员(researcher agent)→ 汇总成带出处的报告。

设计见 docs/design/lead-research-design.md(形态同 Anthropic 多智能体研究系统)。子研究员是完整的
harness run,工具只有 web_search / web_open_url / Read;规划与汇总是两次不带工具的模型调用。
被取消时(用户中断)等待中的子 run 一并中止。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.extension_api.capabilities import AgentRunner
from xana.extension_api.contracts import AgentRunOutcome, SubagentStep
from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.llm_driver.interfaces import ModelRegistry
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolActivity, ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

SUBAGENTS = {"low": 3, "medium": 4, "high": 6}

_PLAN_SYSTEM = (
    "你是研究负责人。把研究问题拆成 {n} 个互不重叠的子课题,交给并行工作的子研究员。每个子课题写一段任务说明:"
    "要查清什么、需要哪些证据(公告、财报、订单、产能、价格等)、交付什么;写具体,划清边界,避免子研究员重复劳动。"
    '只输出 JSON:{{"subtasks": [{{"title": "...", "brief": "..."}}]}}'
)
_SYNTH_SYSTEM = (
    "你是研究负责人。根据子研究员交回的发现,写一份完整回答研究问题的报告(Markdown)。只使用发现里有出处的事实,"
    "引用保留 [标题](url);不同来源矛盾时并列说明;最后列出未能确认的内容。不要编造数字,不要补充发现以外的事实。"
)

_SPEC = ToolSpec(
    toolset="research",
    component=ComponentSpec(
        kind="tool", name="deep_research", qualified_name="builtin.deep_research",
        description=(
            "深度研究:把研究问题拆成若干子课题,由多个子研究员并行联网检索、阅读原文核实,再汇总成带出处的报告"
            "(同步返回,需要几分钟到十几分钟)。适合需要大量检索和交叉核实的问题,如产业链梳理、多家公司对比。\n"
            f"effort:low {SUBAGENTS['low']} 个子研究员,medium {SUBAGENTS['medium']} 个(默认),"
            f"high {SUBAGENTS['high']} 个。question 写清研究目标、范围和需要的证据。"),
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "研究问题:目标、范围、需要的证据"},
            "effort": {"type": "string", "enum": ["low", "medium", "high"], "description": "默认 medium"},
        },
        "required": ["question"],
    },
    read_only=True, open_world=True, concurrency="parallel",
    display=ToolDisplay(category="research", verb="深度研究", summary_args=("question",)),
)


def _parse_plan(text: str, question: str, n: int) -> list[dict[str, str]]:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        subtasks = json.loads(m.group(0))["subtasks"] if m else []
    except (json.JSONDecodeError, KeyError, TypeError):
        subtasks = []
    plan = [{"title": str(s.get("title", "")).strip(), "brief": str(s.get("brief", "")).strip()}
            for s in subtasks if isinstance(s, dict) and (s.get("brief") or s.get("title"))]
    return plan[:n] or [{"title": question[:40], "brief": question}]


class LeadResearchTool(Tool):
    def __init__(self, *, runner: AgentRunner, model_registry: ModelRegistry, provider: str, model: str,
                 findings_root: Path, researcher_agent: str = "researcher") -> None:
        self.runner = runner
        self.model_registry = model_registry
        self.provider = provider
        self.model = model
        self.findings_root = findings_root
        self.researcher_agent = researcher_agent

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not str(invocation.input.get("question") or "").strip():
            raise ValueError("question 不能为空")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="只读联网研究")

    async def _complete(self, system: str, user: str) -> tuple[str, str]:
        """一次不带工具的模型调用,返回 (回答, 思考)。"""
        client = await self.model_registry.get(self.provider)
        request = ModelRequest(provider=self.provider, model=self.model,
                               messages=(ModelMessage(role="system", content=system),
                                         ModelMessage(role="user", content=user)))
        parts: list[str] = []
        thinking: list[str] = []
        async for event in client.stream(request):
            if event.kind == "text_delta":
                (thinking if event.payload.get("channel") == "thinking" else parts).append(
                    event.payload.get("text", ""))
            elif event.kind == "error" and event.error is not None:
                raise RuntimeError(event.error.message)
        return "".join(parts), "".join(thinking)

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        question = str(invocation.input["question"]).strip()
        n = SUBAGENTS.get(str(invocation.input.get("effort") or "medium"), SUBAGENTS["medium"])
        progress = context.report_progress or (lambda _text: None)
        activity = context.report_activity or (lambda _a: None)
        t0 = time.monotonic()
        progress("规划子课题")
        activity(ToolActivity(lane="plan", lane_title="规划", kind="note", text=f"把问题拆成 {n} 个子课题"))
        try:
            plan_text, plan_thinking = await self._complete(_PLAN_SYSTEM.format(n=n), f"研究问题:{question}")
        except RuntimeError as exc:
            activity(ToolActivity(lane="plan", kind="error", text=f"规划失败:{exc}"))
            return ToolResult(ok=False, content=None, failure_kind="execution", error=str(exc),
                              rendered=f"[deep_research] 规划失败:{exc}", retryable=True)
        plan = _parse_plan(plan_text, question, n)
        if plan_thinking.strip():
            activity(ToolActivity(lane="plan", kind="thinking", text=_clip(plan_thinking, THINKING_CLIP)))
        for i, sub in enumerate(plan, 1):
            activity(ToolActivity(lane="plan", kind="done", text=f"{i}. {sub['title']} — {_clip(sub['brief'], 60)}"))

        counts = {"web_search": 0, "web_open_url": 0, "done": 0}

        def report() -> None:
            progress(f"子研究员 {counts['done']}/{len(plan)} 完成 · 搜索 {counts['web_search']} 次 · "
                     f"打开 {counts['web_open_url']} 次网页")

        tag = uuid.uuid4().hex[:6]

        async def one(i: int, sub: dict[str, str]) -> AgentRunOutcome:
            lane = f"r{i + 1}"
            activity(ToolActivity(lane=lane, lane_title=f"子研究员 {i + 1}:{sub['title']}", kind="note",
                                  text="开始"))

            def on_step(step: SubagentStep) -> None:
                if step.kind == "thinking":
                    activity(ToolActivity(lane=lane, kind="thinking", text=step.text))
                elif step.kind == "tool":
                    if step.tool_name in counts:
                        counts[step.tool_name] += 1
                        report()
                    kind = {"web_search": "search", "web_open": "open"}.get(step.category, "tool")
                    label = {"search": "搜索", "open": "打开"}.get(kind, step.tool_name)
                    activity(ToolActivity(lane=lane, kind=kind, text=f"{label}:{step.text}" if step.text else label))

            outcome = await self.runner.run(
                agent_name=self.researcher_agent,
                prompt=f"总研究问题:{question}\n\n你的子课题:{sub['title']}\n{sub['brief']}",
                session_id=f"{context.session_id}-research-{tag}-{i + 1}",
                parent_run_id=context.run_id, parent_session_id=context.session_id, on_step=on_step)
            counts["done"] += 1
            report()
            if outcome.status == "completed":
                calls = sum(outcome.tool_counts.values())
                activity(ToolActivity(lane=lane, kind="done",
                                      text=f"完成 · 工具 {calls} 次 · 发现:{_first_line(outcome.text)}"))
            else:
                activity(ToolActivity(lane=lane, kind="error", text=f"未完成:{outcome.error or outcome.status}"))
            return outcome

        report()
        outcomes = await asyncio.gather(*[one(i, sub) for i, sub in enumerate(plan)])
        sections = []
        for i, (sub, out) in enumerate(zip(plan, outcomes), 1):
            body = out.text if out.status == "completed" and out.text.strip() else f"(未完成:{out.error or out.status})"
            sections.append(f"## 子课题 {i}:{sub['title']}\n{body}")
        findings = "\n\n".join(sections)
        path = self._save(question, plan, findings)
        progress("汇总报告")
        activity(ToolActivity(lane="synth", lane_title="汇总", kind="note", text="根据子研究员的发现撰写报告"))
        try:
            report_text, synth_thinking = await self._complete(
                _SYNTH_SYSTEM, f"研究问题:{question}\n\n子研究员的发现:\n{findings}")
            if synth_thinking.strip():
                activity(ToolActivity(lane="synth", kind="thinking", text=_clip(synth_thinking, THINKING_CLIP)))
            activity(ToolActivity(lane="synth", kind="done", text=f"报告完成 · {len(report_text)} 字"))
        except RuntimeError as exc:
            report_text = f"(汇总失败:{exc};以下为子研究员的原始发现)\n\n{findings}"
            activity(ToolActivity(lane="synth", kind="error", text=f"汇总失败:{exc}"))
        stats: dict[str, Any] = {
            "subagents": len(plan), "completed": sum(o.status == "completed" for o in outcomes),
            "web_search": sum(o.tool_counts.get("web_search", 0) for o in outcomes),
            "web_open_url": sum(o.tool_counts.get("web_open_url", 0) for o in outcomes),
            "elapsed_s": round(time.monotonic() - t0, 1), "findings_file": str(path),
        }
        head = (f"深度研究完成:{stats['completed']}/{stats['subagents']} 个子研究员完成,搜索 {stats['web_search']} 次,"
                f"打开网页 {stats['web_open_url']} 次,用时 {stats['elapsed_s']:.0f} 秒。子研究员的原始发现保存在 "
                f"{path},需要核对时用 read 查看。\n\n")
        return ToolResult(ok=True, content=stats, rendered=head + report_text,
                          metadata={"display_text": f"{stats['subagents']} 个子研究员 · 搜索 {stats['web_search']} 次 · "
                                                    f"打开网页 {stats['web_open_url']} 次 · {stats['elapsed_s']:.0f} 秒"})

    def _save(self, question: str, plan: list[dict[str, str]], findings: str) -> Path:
        d = self.findings_root / time.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"research_{time.strftime('%H%M%S')}_{uuid.uuid4().hex[:6]}.md"
        plan_md = "\n".join(f"{i}. {s['title']}:{s['brief']}" for i, s in enumerate(plan, 1))
        path.write_text(f"# {question}\n\n## 子课题\n\n{plan_md}\n\n{findings}\n", encoding="utf-8")
        return path


THINKING_CLIP = 160


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _first_line(text: str) -> str:
    """发现的首句(跳过标题行),用于时间线里的一行摘要。"""
    for line in text.splitlines():
        line = line.strip().lstrip("#-*> ").strip()
        if line:
            return _clip(line, 80)
    return "(无)"
