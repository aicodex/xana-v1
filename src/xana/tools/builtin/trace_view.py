"""trace_view:回看一次 run 的完整过程(docs/design/iteration-agent-design.md §7.3)。

先给摘要(每步一行:模型调用的耗时 / 用量 / 停止原因与发出的工具调用,工具的成败 / 耗时 / 错误),
再按 steps 展开指定步骤的全文;不把整份记录塞进上下文。数据来自 runs/<run_id>.jsonl 与 RunRecord。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.state.file_runs import prompt_path, read_run_transcript
from xana.state.interfaces import RunStore
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

TRACE_VIEW_TOOL = "trace_view"
_SUMMARY_MAX_STEPS = 300      # 摘要最多列这么多步,超出的提示用 steps 分段看
_STEP_MAX_CHARS = 20_000      # 展开时单步最多这么多字符
_LINE_CHARS = 100

_DESCRIPTION = (
    "回看一次 run(某个 agent 的一次运行)的完整过程,用于排查它为什么得出某个结果。\n\n"
    "- 只给 run_id:返回摘要——运行状态、总用时与用量、子 run 列表,以及每一步一行"
    "(模型调用的耗时、用量、停止原因和发出的工具调用;工具的成败、耗时、错误或输出首行)。\n"
    "- 再给 steps(如 \"5\" 或 \"5-9\"):展开这些步骤的全文,包括模型的思考、完整工具参数与工具输出。\n"
    "- 给 prompt(摘要里模型步骤的 sys 值):返回该次调用使用的 system prompt 全文。\n\n"
    "先看摘要定位可疑步骤,再只展开需要的几步。子 run 用它自己的 run_id 再看。"
)

_SPEC = ToolSpec(
    temporal="static",
    toolset="eval",
    component=ComponentSpec(
        kind="tool", name=TRACE_VIEW_TOOL, qualified_name="builtin.trace_view",
        description=_DESCRIPTION,
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "要查看的 run"},
            "steps": {"type": "string", "description": "要展开的步骤:单个编号或闭区间,如 \"5\"、\"5-9\""},
            "prompt": {"type": "string", "description": "system prompt 的 sha(摘要里的 sys 值),返回其全文"},
        },
        "required": ["run_id"],
    },
    read_only=True,
    concurrency="parallel",
    display=ToolDisplay(category="read", verb="查看运行记录", summary_args=("run_id", "steps")),
)


def _one_line(text: str, limit: int = _LINE_CHARS) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _secs(ms: Any) -> str:
    return f"{ms / 1000:.1f}s" if isinstance(ms, (int, float)) else "?"


def _parse_steps(spec: str, n: int) -> range:
    a, _, b = spec.strip().partition("-")
    lo = int(a)
    hi = int(b) if b.strip() else lo
    if lo < 1 or hi < lo or lo > n:
        raise ValueError(f"steps 超出范围:共 {n} 步")
    return range(lo, min(hi, n) + 1)


def _call_args(tc: dict) -> str:
    inp = tc.get("input") or {}
    args = ", ".join(f"{k}={_one_line(json.dumps(v, ensure_ascii=False), 40)}" for k, v in inp.items())
    return f"{tc.get('tool_name', '?')}({args})"


def _summary_line(i: int, row: dict) -> str:
    p = row["payload"]
    meta = p.get("metadata") or {}
    trace = (row.get("metadata") or {}).get("trace") or {}
    role = p.get("role")
    if role == "user":
        kind = meta.get("note_kind")
        return f"#{i} 注记[{kind}] {_one_line(meta.get('note_text') or p.get('content', ''), 60)}" if kind \
            else f"#{i} 用户 {_one_line(p.get('content', ''))}"
    if role == "assistant":
        u = trace.get("usage") or {}
        cost = (f" in={u.get('input', 0) + u.get('cache_read', 0)}(缓存 {u.get('cache_read', 0)}) out={u.get('output', 0)}"
                if u else "")
        head = f"#{i} 模型 {_secs(trace.get('latency_ms'))}{cost}"
        if trace.get("stop_reason"):
            head += f" stop={trace['stop_reason']}"
        if trace.get("system_sha"):
            head += f" sys={trace['system_sha']}"
        calls = meta.get("tool_calls") or []
        parts = [head]
        if p.get("content"):
            parts.append(_one_line(p["content"], 80))
        if calls:
            parts.append("调用 " + "; ".join(_call_args(tc) for tc in calls))
        return " · ".join(parts)
    if role == "tool":
        mark = "✓" if meta.get("ok", True) else "✗"
        detail = trace.get("error") or next((ln for ln in str(p.get("content", "")).splitlines() if ln.strip()), "")
        return f"#{i} 工具 {meta.get('tool_name', '?')} {mark} {_secs(trace.get('elapsed_ms'))} · {_one_line(detail, 80)}"
    return f"#{i} {role} {_one_line(p.get('content', ''))}"


def _expand(i: int, row: dict) -> str:
    p = row["payload"]
    meta = p.get("metadata") or {}
    trace = (row.get("metadata") or {}).get("trace") or {}
    out = [f"── #{i} {p.get('role')} ──"]
    if trace:
        out.append("trace: " + json.dumps(trace, ensure_ascii=False))
    if meta.get("reasoning_content"):
        out.append("[思考]\n" + meta["reasoning_content"])
    if p.get("content"):
        out.append(("[输出]\n" if p.get("role") == "tool" else "[正文]\n") + str(p["content"]))
    for tc in meta.get("tool_calls") or []:
        out.append(f"[调用 {tc.get('tool_name')} id={tc.get('call_id')}]\n"
                   + json.dumps(tc.get("input") or {}, ensure_ascii=False, indent=1))
    text = "\n".join(out)
    if len(text) > _STEP_MAX_CHARS:
        text = text[:_STEP_MAX_CHARS] + f"\n…[本步共 {len(text)} 字符,只显示前 {_STEP_MAX_CHARS}]"
    return text


class TraceViewTool(Tool):
    def __init__(self, *, run_store: RunStore, runs_dir: Path) -> None:
        self._runs = run_store
        self._dir = Path(runs_dir)       # sessions/runs:run 记录与 prompts/

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not str(invocation.input.get("run_id") or "").strip():
            raise ValueError("trace_view 需要 run_id")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="read-only trace")

    def _fail(self, msg: str) -> ToolResult:
        return ToolResult(ok=False, content=None, error=msg, failure_kind="not_found", rendered=msg)

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        inp = invocation.input
        sha = str(inp.get("prompt") or "").strip()
        if sha:
            path = prompt_path(self._dir, sha)
            if not path.exists():
                return self._fail(f"没有 sha 为 {sha} 的 system prompt 存档")
            text = path.read_text(encoding="utf-8")
            return ToolResult(ok=True, content=text, rendered=text)
        run_id = str(inp["run_id"]).strip()
        loaded = read_run_transcript(self._dir, run_id)
        if loaded is None:
            return self._fail(f"没有 run {run_id} 的记录")
        rows, end_status = loaded
        if inp.get("steps"):
            try:
                picked = _parse_steps(str(inp["steps"]), len(rows))
            except ValueError as exc:
                return ToolResult(ok=False, content=None, error=str(exc), failure_kind="validation",
                                  rendered=str(exc))
            text = "\n\n".join(_expand(i, rows[i - 1]) for i in picked)
            return ToolResult(ok=True, content=text, rendered=text)
        text = await self._summary(run_id, rows, end_status)
        return ToolResult(ok=True, content=text, rendered=text)

    async def _summary(self, run_id: str, rows: list[dict], end_status: str | None) -> str:
        rec = await self._runs.get(run_id)
        lines: list[str] = []
        if rec is not None:
            md = rec.metadata
            dur = (f" · 用时 {md['finished_at'] - md['started_at']:.1f}s"
                   if isinstance(md.get("finished_at"), (int, float)) and isinstance(md.get("started_at"), (int, float))
                   else "")
            lines.append(f"run {run_id} · agent {rec.agent_name} · {rec.status}{dur}"
                         + (f" · 父 run {rec.parent_run_id}" if rec.parent_run_id else ""))
            u = rec.usage
            if u.model_calls:
                lines.append(f"模型调用 {u.model_calls} 次 · 输入 {u.total_input_tokens}"
                             f"(缓存命中 {u.total_cache_read_tokens})· 输出 {u.total_output_tokens} · 模型 {u.model}")
            for key in ("budget_exceeded", "episode_exceeded"):
                if md.get(key):
                    lines.append(f"{key}: {md[key]}")
            if rec.error:
                lines.append(f"错误: {rec.error}")
            lines.append("输入: " + _one_line(rec.input_request.get("text", ""), 300))
            children = [r for r in await self._runs.list_all() if r.parent_run_id == run_id]
            if children:
                lines.append("子 run: " + "; ".join(
                    f"{c.run_id}({c.agent_name}, {c.status}, {_one_line(c.label, 20)})" for c in children))
        else:
            lines.append(f"run {run_id}(无 RunRecord)")
        lines.append(f"共 {len(rows)} 步:")
        for i, row in enumerate(rows[:_SUMMARY_MAX_STEPS], start=1):
            lines.append(_summary_line(i, row))
        if len(rows) > _SUMMARY_MAX_STEPS:
            lines.append(f"…另有 {len(rows) - _SUMMARY_MAX_STEPS} 步未列出,用 steps 分段查看")
        lines.append(f"结束: {end_status or '未写终止标记(运行中或异常退出)'}")
        lines.append('用 steps="N" 或 "N-M" 展开步骤全文。')
        return "\n".join(lines)
