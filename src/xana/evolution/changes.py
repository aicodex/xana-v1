"""变更单写入 —— L2/changes/ 的门禁写入(计划 §6.1)。

ChangeWriter 内部持有 GatedEntryWriter(change_gate.py):create 查重、supersede
链、frontmatter(status/evidence)。

schema(§6.1):假设 → diff 摘要 → 证据指针 → 判决。
"""
from __future__ import annotations

from xana.evolution.contracts import ChangeBody
from xana.memory.interfaces import MemorySearchEngine
from xana.memory.markdown import MarkdownMemoryTree
from xana.evolution.change_gate import GatedEntryWriter, _split_frontmatter
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult

CHANGES_DIR = "L2/changes"


def render_change_body(
    *, hypothesis: str, diff_summary: str, evidence: str, verdict: str = "pending",
) -> str:
    """变更单正文:假设 → diff 摘要 → 证据指针 → 判决。"""
    return (
        f"## 假设\n\n{hypothesis.strip()}\n\n"
        f"## diff 摘要\n\n{diff_summary.strip()}\n\n"
        f"## 证据指针\n\n{evidence.strip() or '(待补)'}\n\n"
        f"## 判决\n\n{verdict}\n"
    )


def parse_change_body(body: str) -> ChangeBody:
    """从正文读回四段(缺失为空串/默认 pending)。"""
    sections: dict[str, list[str]] = {}
    current = ""
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line)
    out: dict[str, str] = {}
    for key, value in sections.items():
        out[key] = "\n".join(value).strip()
    return ChangeBody(
        hypothesis=out.get("假设", ""),
        diff_summary=out.get("diff 摘要", ""),
        evidence=out.get("证据指针", ""),
        verdict=out.get("判决", "") or "pending",
    )


class ChangeWriter:
    """变更单写入口:propose(create/supersede) + judge(refine 判决段)。"""

    def __init__(
        self,
        *,
        tree: MarkdownMemoryTree,
        engine: MemorySearchEngine,
        changes_dir: str = CHANGES_DIR,
    ) -> None:
        self._tree = tree
        self._tool = GatedEntryWriter(tree=tree, engine=engine, rules_dir=changes_dir)
        self._dir = changes_dir

    async def propose(
        self, invocation: ToolInvocation, context: ToolContext,
    ) -> ToolResult:
        """input: path / hypothesis / diff_summary / evidence [/ supersedes]。"""
        inp = invocation.input
        name = str(inp.get("path") or "").strip()
        if not name:
            return ToolResult(ok=False, content=None, error="变更单缺 path",
                              failure_kind="validation", rendered="[change denied] 缺 path")
        title = f"变更单 {name}"
        body = render_change_body(
            hypothesis=str(inp.get("hypothesis") or ""),
            diff_summary=str(inp.get("diff_summary") or ""),
            evidence=str(inp.get("evidence") or ""),
            verdict="pending",
        )
        supersedes = str(inp.get("supersedes") or "").strip()
        action = "supersede" if supersedes else "create"
        inner = ToolInvocation(
            tool_name="memory_write",
            input={
                "action": action, "path": name, "title": title, "content": body,
                **({"supersedes": supersedes} if supersedes else {}),
                **({"evidence": str(inp.get("evidence") or "")} if inp.get("evidence") else {}),
            },
            call_id=invocation.call_id, session_id=invocation.session_id,
        )
        return await self._tool.execute(inner, context)

    async def judge(
        self, invocation: ToolInvocation, context: ToolContext,
    ) -> ToolResult:
        """input: path / verdict ∈ {accepted, rejected}。refine 判决段,状态门禁保留。"""
        inp = invocation.input
        name = str(inp.get("path") or "").strip()
        verdict = str(inp.get("verdict") or "").strip()
        if verdict not in ("accepted", "rejected"):
            return ToolResult(
                ok=False, content=None,
                error=f"判决必须是 accepted/rejected, got {verdict!r}",
                failure_kind="validation", rendered="[change denied] 非法判决",
            )
        rel = f"{self._dir}/{name if name.endswith('.md') else name + '.md'}"
        path = self._tree.root / rel
        if not path.exists():
            return ToolResult(ok=False, content=None, error=f"变更单不存在: {rel}",
                              failure_kind="not_found", rendered=f"[change failed] {rel} not found")
        _fm, old_body = _split_frontmatter(path.read_text(encoding="utf-8"))
        fields = parse_change_body(old_body)
        if not fields.hypothesis:
            return ToolResult(ok=False, content=None,
                              error=f"变更单 {rel} 正文缺 假设 段,不是合法 schema",
                              failure_kind="validation", rendered="[change failed] bad schema")
        new_body = render_change_body(
            hypothesis=fields.hypothesis,
            diff_summary=fields.diff_summary,
            evidence=fields.evidence,
            verdict=verdict,
        )
        inner = ToolInvocation(
            tool_name="memory_write",
            input={"action": "refine", "path": name, "content": new_body},
            call_id=invocation.call_id, session_id=invocation.session_id,
        )
        return await self._tool.execute(inner, context)
