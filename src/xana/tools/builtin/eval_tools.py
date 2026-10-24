"""eval_run / eval_score:按评测套件跑样本、调套件的打分命令(docs/design/iteration-agent-design.md §8、§9.2)。

套件在 <项目>/.xana/evals/<名>/(格式见 xana/evolution/evals.py);评测 run 的工具环境 = 样本的研究时点
+ 套件策略(回放 / 时点截断 / 拒绝),每个样本一个独立会话,trace 可用 trace_view 查看。
迭代者(iterator)看留出集只得到汇总指标,不得到样本细节(§9.3)。
"""
from __future__ import annotations

import asyncio
import json

from xana.agents.iterator import ITERATION_TEAM
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.evolution.evals import EvalRunner, SuiteError, compare_batches
from xana.harness_sdk.background import BackgroundTasks, TaskOutcome
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

# 迭代者与它的子 agent 看留出集只得到汇总(子 agent 看到细节会转述给迭代者)
_HELDOUT_BLIND = ITERATION_TEAM


def _spec(name: str, description: str, props: dict, required: list[str], verb: str) -> ToolSpec:
    return ToolSpec(
        temporal="static",
        toolset="eval",
        component=ComponentSpec(kind="tool", name=name, qualified_name=f"builtin.{name}", description=description,
                                source=ComponentSource(source_type="builtin", scope="system")),
        input_schema={"type": "object", "properties": props, "required": required},
        concurrency="serialized",
        display=ToolDisplay(category="other", verb=verb, summary_args=("suite", "split", "batch")),
    )


_RUN_SPEC = _spec(
    "eval_run",
    "按评测套件跑一批样本:每个样本在独立会话里运行被测 agent,工具环境截到样本的研究时点(as_of)。"
    "返回批次号与每个样本的运行状态、run_id(可用 trace_view 查看过程)、用时与用量。跑完后用 eval_score 打分。\n"
    "- split:train / heldout / live;或用 case_ids 指定样本。repeat:每个样本重复几次(估计噪声)。\n"
    "- agent:被测 agent(默认套件指定的;对比候选版本时传候选 agent 名)。tape:回放用的 run_id 列表(可选)。\n"
    "- 先等待 wait_s 秒(默认 600):没跑完就转为后台任务,返回任务 id;用 task_wait 等,或先做别的事,结束时结果会自动送到这里。",
    {"suite": {"type": "string"}, "split": {"type": "string", "enum": ["train", "heldout", "live"]},
     "case_ids": {"type": "array", "items": {"type": "string"}}, "repeat": {"type": "integer", "minimum": 1, "maximum": 5},
     "agent": {"type": "string"}, "label": {"type": "string", "description": "批次标签,如 v1-baseline"},
     "tape": {"type": "array", "items": {"type": "string"}},
     "wait_s": {"type": "integer", "minimum": 0, "maximum": 1800}},
    ["suite"], "运行评测")

_SCORE_SPEC = _spec(
    "eval_score",
    "调评测套件的打分命令给一个批次打分(确定性脚本,不用模型),返回汇总指标(分训练 / 留出 / 实时集)、"
    "与基线的对比,以及每个样本的结果(实际值、误差、是否大偏差及类型)。分数只认这个工具的输出。",
    {"suite": {"type": "string"}, "batch": {"type": "string", "description": "eval_run 返回的批次号;latest = 最近一批"}},
    ["suite", "batch"], "评测打分")


def _fail(tool: str, msg: str) -> ToolResult:
    return ToolResult(ok=False, content=None, error=msg, failure_kind="validation", rendered=f"[{tool}] {msg}")


class EvalRunTool(Tool):
    def __init__(self, evals: EvalRunner, background: BackgroundTasks | None = None) -> None:
        self._evals = evals
        self._background = background

    def spec(self) -> ToolSpec:
        return _RUN_SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="eval_run")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        inp = invocation.input
        try:
            suite = self._evals.suite(str(inp["suite"]))
            cases = suite.select(split=inp.get("split"), case_ids=tuple(inp.get("case_ids") or ()))
        except SuiteError as exc:
            return _fail("eval_run", str(exc))
        if not cases:
            return _fail("eval_run", "没有选中任何样本")
        repeat = int(inp.get("repeat") or 1)
        if context.report_progress:
            context.report_progress(f"评测 {suite.name}:{len(cases)} 个样本 × {repeat} 次")

        async def run_batch(progress=None) -> TaskOutcome:
            batch = await self._evals.run(suite, cases, repeat=repeat, agent=inp.get("agent"),
                                          label=str(inp.get("label") or ""), tape=tuple(inp.get("tape") or ()),
                                          on_case=progress)
            rows = self._evals.batch_runs(suite, batch)
            lines = [f"[eval_run] 套件 {suite.name} · 批次 {batch} · {len(rows)} 次运行"]
            blind = context.agent_name in _HELDOUT_BLIND
            hidden = [r for r in rows if blind and r.get("split") == "heldout"]
            if hidden:
                lines.append(f"- 留出集 {len(hidden)} 次运行:完成 {sum(r['status'] == 'completed' for r in hidden)}"
                             "(逐样本细节不提供,看 eval_score / eval_compare 的汇总)")
            for r in sorted((r for r in rows if r not in hidden), key=lambda r: (r["case_id"], r["rep"])):
                lines.append(f"- {r['case_id']}#{r['rep']} {r['status']} · run {r['run_id']} · {r['elapsed_s']}s · "
                             f"模型调用 {r['model_calls']} 次 · 输入 {r['tokens_in']} 输出 {r['tokens_out']}"
                             + (f" · 错误 {r['error']}" if r.get("error") else ""))
            lines.append(f"用 eval_score(suite=\"{suite.name}\", batch=\"{batch}\") 打分。")
            return TaskOutcome(ok=True, rendered="\n".join(lines), content={"batch": batch, "runs": len(rows)})

        if self._background is None:
            out = await run_batch()
            return ToolResult(ok=True, content=out.content, rendered=out.rendered)
        rec = self._background.start(kind="eval", label=f"{suite.name}:{inp.get('label') or inp.get('split') or 'cases'}",
                                     parent_session=context.session_id, parent_run=context.run_id or "",
                                     run=run_batch)
        try:
            rec = await self._background.wait(rec.id, min(int(inp.get("wait_s", 600)), 1800))
        except asyncio.CancelledError:
            self._background.stop(rec.id)
            raise
        if rec.status == "running":
            return ToolResult(ok=True, content={"task_id": rec.id, "status": "running"}, rendered=(
                f"[eval_run] 批次仍在运行,转为后台任务 {rec.id}。进度:{'; '.join(rec.progress[-3:]) or '无'}。\n"
                f"用 task_wait(task_id=\"{rec.id}\") 等它,或先做别的事;结束时结果会自动送到这里。"))
        return ToolResult(ok=rec.status == "completed", content=rec.content, rendered=rec.rendered,
                          error=None if rec.status == "completed" else rec.status,
                          failure_kind=None if rec.status == "completed" else "execution")


class EvalScoreTool(Tool):
    def __init__(self, evals: EvalRunner) -> None:
        self._evals = evals

    def spec(self) -> ToolSpec:
        return _SCORE_SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="eval_score")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            suite = self._evals.suite(str(invocation.input["suite"]))
            result = await self._evals.score(suite, str(invocation.input["batch"]))
        except SuiteError as exc:
            return _fail("eval_score", str(exc))
        per_case = result.get("per_case") or []
        if context.agent_name in _HELDOUT_BLIND:   # 留出集只给汇总,样本细节不回流给迭代者
            per_case = [r for r in per_case if r.get("split") != "heldout"]
        text = (f"[eval_score] 批次 {result.get('batch')} 汇总:\n" + json.dumps(result.get("summary") or {}, ensure_ascii=False, indent=1)
                + "\n\n样本结果:\n" + "\n".join(json.dumps(r, ensure_ascii=False) for r in per_case))
        return ToolResult(ok=True, content={"summary": result.get("summary")}, rendered=text)


_COMPARE_SPEC = _spec(
    "eval_compare",
    "配对对比两个已打分批次(基线 vs 候选,同一批样本):逐样本均值 ± 标准差与差值,差值超过 2 倍噪声才算改善 / 变坏"
    "(噪声取两边重复运行的标准差,没有重复时用默认值,所以要判断小差异就用 repeat ≥ 2);"
    "列出变坏的样本(必须保持的样本出现在这里就一票否决)和两边打分次数不一致的样本(不一致时对比不可信)。"
    "迭代者看不到留出集的逐样本对比,只有汇总。",
    {"suite": {"type": "string"}, "base": {"type": "string", "description": "基线批次号"},
     "candidate": {"type": "string", "description": "候选批次号"}},
    ["suite", "base", "candidate"], "对比批次")


class EvalCompareTool(Tool):
    def __init__(self, evals: EvalRunner) -> None:
        self._evals = evals

    def spec(self) -> ToolSpec:
        return _COMPARE_SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="eval_compare")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        inp = invocation.input
        try:
            suite = self._evals.suite(str(inp["suite"]))
            base = await self._evals.score(suite, str(inp["base"]))
            cand = await self._evals.score(suite, str(inp["candidate"]))
        except SuiteError as exc:
            return _fail("eval_compare", str(exc))
        if context.agent_name in _HELDOUT_BLIND:   # 留出集只给汇总
            heldout = {c.id for c in suite.cases if c.split == "heldout"}
            full = compare_batches(base, cand)
            visible = compare_batches(
                {"per_case": [r for r in base.get("per_case", []) if r.get("case_id") not in heldout]},
                {"per_case": [r for r in cand.get("per_case", []) if r.get("case_id") not in heldout]})
            held = compare_batches(
                {"per_case": [r for r in base.get("per_case", []) if r.get("case_id") in heldout]},
                {"per_case": [r for r in cand.get("per_case", []) if r.get("case_id") in heldout]})
            heldout_summary = {k: (len(v) if isinstance(v, list) else v) for k, v in held.items() if k != "per_case"}
            result = {"heldout": {**heldout_summary,
                                  "note": "留出集只给汇总:improved / worse / imbalanced_cases 是样本个数;"
                                          "paired_cases 为 0 表示两个批次没有共同的留出集样本"},
                      "train_and_live": visible, "trustworthy": full["trustworthy"]}
        else:
            result = compare_batches(base, cand)
        return ToolResult(ok=True, content={k: v for k, v in result.items() if k != "per_case"},
                          rendered="[eval_compare]\n" + json.dumps(result, ensure_ascii=False, indent=1))


_DESCRIBE_SPEC = _spec(
    "eval_describe",
    "查看评测套件的说明:评测量的是什么、指标怎么算、基线是什么,以及样本构成(训练集样本 id、留出 / 实时集数量)。"
    "审视评测是否量到了目标的关键组成时用它(打分代码与真值不可见)。",
    {"suite": {"type": "string"}}, ["suite"], "查看评测说明")


class EvalDescribeTool(Tool):
    def __init__(self, evals: EvalRunner) -> None:
        self._evals = evals

    def spec(self) -> ToolSpec:
        return _DESCRIBE_SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="eval_describe")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            suite = self._evals.suite(str(invocation.input["suite"]))
        except SuiteError as exc:
            return _fail("eval_describe", str(exc))
        by_split: dict[str, list[str]] = {}
        for c in suite.cases:
            by_split.setdefault(c.split, []).append(c.id)
        lines = [f"# 评测套件 {suite.name}(被测 agent:{suite.agent})", suite.metrics_doc or "(套件没有 METRICS.md)",
                 "## 样本构成"]
        for split, ids in by_split.items():
            if split == "heldout" and context.agent_name in _HELDOUT_BLIND:
                lines.append(f"- {split}:{len(ids)} 个(样本不列出)")
            else:
                lines.append(f"- {split}:{len(ids)} 个 — {', '.join(ids)}")
        return ToolResult(ok=True, content={k: len(v) for k, v in by_split.items()}, rendered="\n".join(lines))
