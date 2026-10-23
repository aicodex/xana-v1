"""agent_version / ledger:迭代者改 agent 与记账(docs/design/iteration-agent-design.md §9.2)。

- agent_version:只改工作流正文与 playbook.md(目标契约原样保留);候选 → eval_run 对比 → 采纳或丢弃;可回滚。
- ledger:只追加的迭代账本(假设、改动、归因、证据、判决),读最近的记录。
"""
from __future__ import annotations

import json
import time
import uuid

from xana.agents.versions import AgentVersions, VersionError
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.evolution.contracts import LedgerEntry
from xana.evolution.ledger import Ledger
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


def _spec(name: str, description: str, props: dict, required: list[str], verb: str, destructive: bool) -> ToolSpec:
    return ToolSpec(
        temporal="static",
        destructive=destructive,
        toolset="eval",
        component=ComponentSpec(kind="tool", name=name, qualified_name=f"builtin.{name}", description=description,
                                source=ComponentSource(source_type="builtin", scope="system")),
        input_schema={"type": "object", "properties": props, "required": required},
        display=ToolDisplay(category="other", verb=verb, summary_args=("action", "name")),
    )


_VERSION_SPEC = _spec(
    "agent_version",
    "管理一个文件 agent 的版本。只能改工作流正文(body)与经验条目(playbook);frontmatter 里的目标契约、工具、"
    "评测套件原样保留,需要改它们只能写目标修改提议给用户。\n"
    "- show:当前 frontmatter(只读)、正文、经验条目、存档版本、候选。\n"
    "- candidate:基于当前版本建候选 <名>-c<N>(body / playbook 不给则沿用当前),返回候选名;"
    "用 eval_run(agent=候选名) 与当前版本对比。\n"
    "- promote:采纳候选(当前版本先存档)。discard:丢弃候选。rollback:恢复某个存档版本(version 如 v2)。",
    {"action": {"type": "string", "enum": ["show", "candidate", "promote", "discard", "rollback"]},
     "name": {"type": "string", "description": "agent 名(promote / discard 传候选名)"},
     "body": {"type": "string", "description": "candidate:新的工作流正文(不含 frontmatter)"},
     "playbook": {"type": "string", "description": "candidate:新的经验条目全文(markdown 列表,每条注明来源样本)"},
     "note": {"type": "string", "description": "candidate:这个候选改了什么、依据哪些样本"},
     "version": {"type": "string", "description": "rollback:存档版本,如 v2"}},
    ["action", "name"], "管理版本", destructive=True)

_LEDGER_SPEC = _spec(
    "ledger",
    "迭代账本(只追加)。append:记一轮迭代——hypothesis(假设)、change(改了什么)、attribution(归因类别)、"
    "evidence(批次号与前后指标)、verdict(kept 保留 / rolled_back 回滚 / abandoned 放弃)。"
    "recent:读最近 n 条,开始新一轮前先看以往的假设与判决,避免重复试错。",
    {"action": {"type": "string", "enum": ["append", "recent"]},
     "name": {"type": "string", "description": "被迭代的 agent"},
     "hypothesis": {"type": "string"}, "change": {"type": "string"}, "attribution": {"type": "string"},
     "evidence": {"type": "object"}, "verdict": {"type": "string", "enum": ["kept", "rolled_back", "abandoned"]},
     "n": {"type": "integer", "minimum": 1, "maximum": 50}},
    ["action"], "记账", destructive=True)


def _fail(tool: str, msg: str) -> ToolResult:
    return ToolResult(ok=False, content=None, error=msg, failure_kind="validation", rendered=f"[{tool}] {msg}")


class AgentVersionTool(Tool):
    def __init__(self, versions: AgentVersions) -> None:
        self._v = versions

    def spec(self) -> ToolSpec:
        return _VERSION_SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="agent_version")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        inp = invocation.input
        action, name = inp.get("action"), str(inp.get("name") or "")
        try:
            if action == "show":
                info = await self._v.show(name)
                text = (f"# {name}({info['path']})\n## frontmatter(只读)\n"
                        + json.dumps(info["frontmatter"], ensure_ascii=False, indent=1)
                        + f"\n## 工作流正文\n{info['body']}\n## 经验条目(playbook.md)\n{info['playbook'] or '(空)'}"
                        + f"\n## 存档版本:{', '.join(info['versions']) or '无'};候选:{', '.join(info['candidates']) or '无'}")
                return ToolResult(ok=True, content=info, rendered=text)
            if action == "candidate":
                cand = await self._v.candidate(name, body=inp.get("body"), playbook=inp.get("playbook"),
                                               note=str(inp.get("note") or ""))
                return ToolResult(ok=True, content={"candidate": cand}, rendered=(
                    f"[agent_version] 已建候选 {cand}。用 eval_run(agent=\"{cand}\") 跑,与当前版本对比后 promote 或 discard。"))
            if action == "promote":
                base, archived = await self._v.promote(name)
                return ToolResult(ok=True, content={"agent": base, "archived": archived},
                                  rendered=f"[agent_version] 已采纳 {name} 为 {base} 的当前版本;原版本存档为 {archived}。")
            if action == "discard":
                await self._v.discard(name)
                return ToolResult(ok=True, content=None, rendered=f"[agent_version] 已丢弃候选 {name}")
            if action == "rollback":
                archived = await self._v.rollback(name, str(inp.get("version") or ""))
                return ToolResult(ok=True, content={"archived": archived}, rendered=(
                    f"[agent_version] 已把 {name} 恢复到 {inp.get('version')};回滚前的版本存档为 {archived}。"))
        except VersionError as exc:
            return _fail("agent_version", str(exc))
        return _fail("agent_version", "action 必须是 show / candidate / promote / discard / rollback")


class LedgerTool(Tool):
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def spec(self) -> ToolSpec:
        return _LEDGER_SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="ledger")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        inp = invocation.input
        if inp.get("action") == "recent":
            entries = await self._ledger.recent(int(inp.get("n") or 10))
            if inp.get("name"):
                entries = tuple(e for e in entries if e.agent == inp["name"])
            text = "\n".join(json.dumps(e.to_json(), ensure_ascii=False) for e in entries) or "(账本为空)"
            return ToolResult(ok=True, content=len(entries), rendered=text)
        if inp.get("action") == "append":
            entry = LedgerEntry(
                episode_id=f"it-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}",
                goal=str(inp.get("hypothesis") or ""), started_at=time.time(), verdict=inp.get("verdict"),
                agent=str(inp.get("name") or ""), hypothesis=str(inp.get("hypothesis") or ""),
                change=str(inp.get("change") or ""), attribution=str(inp.get("attribution") or ""),
                evidence=dict(inp.get("evidence") or {}))
            try:
                await self._ledger.append(entry)
            except ValueError as exc:
                return _fail("ledger", str(exc))
            return ToolResult(ok=True, content={"id": entry.episode_id}, rendered=f"[ledger] 已记录 {entry.episode_id}")
        return _fail("ledger", "action 必须是 append / recent")
