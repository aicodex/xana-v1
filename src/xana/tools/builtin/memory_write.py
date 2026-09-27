"""memory_write: the single gated write entry for L2 rule entries.

Rules live at ``{memory_root}/L2/rules/*.md`` and reuse the MemoryTree node
space. Bare Write/Edit into L2/ is denied at the policy checkpoint; this
tool is the only sanctioned mutation path:

* ``create``  — forces a top-k similarity search first; a hit above the
  threshold is rejected with a demand to use refine/supersede instead.
* ``refine``  — rewrites the body of an existing entry (status preserved).
* ``supersede`` — writes the new entry and flips the old one's frontmatter
  ``status: deprecated`` + ``superseded_by`` (the file is never deleted);
  deprecated entries drop out of search results.

Frontmatter contract: ``title / status / evidence / supersedes / updated``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.memory.contracts import SearchQuery
from xana.memory.interfaces import MemorySearchEngine
from xana.memory.markdown import MarkdownMemoryTree, search_terms
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

_ACTIONS = ("create", "refine", "supersede")

_SPEC = ToolSpec(
    toolset="memory",
    component=ComponentSpec(
        kind="tool",
        name="memory_write",
        qualified_name="builtin.memory_write",
        description=(
            "Gated write entry for L2 rule entries (L2/rules/*.md). "
            "action=create forces a duplicate check; use refine to update an "
            "existing entry and supersede to replace one (old entry becomes "
            "deprecated, never deleted)."
        ),
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_ACTIONS)},
            "path": {"type": "string", "description": "Entry name relative to L2/rules/ (with or without .md)"},
            "title": {"type": "string"},
            "content": {"type": "string", "description": "Rule body (markdown)"},
            "supersedes": {"type": "string", "description": "For action=supersede: existing entry path being replaced"},
            "evidence": {"type": "string", "description": "Run pointer backing this rule, e.g. runs/r12.jsonl"},
        },
        "required": ["action", "path"],
    },
    destructive=True,
    display=ToolDisplay(category="memory", verb="写记忆", summary_args=("title", "path")),
)


def _token_set(text: str) -> set[str]:
    return set(search_terms(text))


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            try:
                parsed = yaml.safe_load("\n".join(lines[1:idx]))
            except yaml.YAMLError:
                parsed = None
            fm = parsed if isinstance(parsed, dict) else {}
            return fm, "\n".join(lines[idx + 1:])
    return {}, text


def _dump_entry(frontmatter: dict[str, Any], body: str) -> str:
    head = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{head}\n---\n\n{body.strip()}\n"


class MemoryWriteTool(Tool):
    def __init__(
        self,
        *,
        tree: MarkdownMemoryTree,
        engine: MemorySearchEngine,
        rules_dir: str = "L2/rules",
        similarity_threshold: float = 0.6,
    ) -> None:
        self._tree = tree
        self._engine = engine
        self._rules_dir = rules_dir.strip("/")
        self._threshold = similarity_threshold

    def spec(self) -> ToolSpec:
        return _SPEC

    # ---- validation -------------------------------------------------------

    async def validate(self, invocation: ToolInvocation) -> None:
        action = invocation.input.get("action")
        if action not in _ACTIONS:
            raise ValueError(f"memory_write action must be one of {_ACTIONS}")
        self._normalize_path(invocation.input.get("path"))
        if action in ("create", "supersede"):
            if not str(invocation.input.get("title") or "").strip():
                raise ValueError(f"memory_write {action} requires non-empty 'title'")
            if not str(invocation.input.get("content") or "").strip():
                raise ValueError(f"memory_write {action} requires non-empty 'content'")
        if action == "refine" and not str(invocation.input.get("content") or "").strip():
            raise ValueError("memory_write refine requires non-empty 'content'")
        if action == "supersede" and not str(invocation.input.get("supersedes") or "").strip():
            raise ValueError("memory_write supersede requires 'supersedes' (path of the entry being replaced)")

    def _normalize_path(self, raw: Any) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("memory_write requires non-empty 'path'")
        name = raw.strip().replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"unsafe rule path: {raw!r}")
        name = name.removeprefix(self._rules_dir + "/")
        if not name.endswith(".md"):
            name = f"{name}.md"
        if not Path(name).name or ".." in Path(name).parts:
            raise ValueError(f"unsafe rule path: {raw!r}")
        return f"{self._rules_dir}/{name}"

    # ---- permission: this tool IS the sanctioned write path ---------------

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="memory_write is the gated rule write entry")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    # ---- execution --------------------------------------------------------

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        action = invocation.input["action"]
        rel = self._normalize_path(invocation.input["path"])
        title = str(invocation.input.get("title") or "").strip()
        content = str(invocation.input.get("content") or "").strip()
        evidence = str(invocation.input.get("evidence") or "").strip()

        if action == "create":
            return await self._create(rel, title, content, evidence)
        if action == "refine":
            return await self._refine(rel, content)
        return await self._supersede(rel, title, content, evidence, invocation)

    async def _create(self, rel: str, title: str, content: str, evidence: str) -> ToolResult:
        dup = await self._find_duplicate(rel, title, content)
        if dup is not None:
            sim, hit_path = dup
            return ToolResult(
                ok=False,
                content=None,
                error=(
                    f"相似条目已存在 ({hit_path}, 相似度 {sim:.2f} ≥ {self._threshold:.2f})。"
                    f"改用 action=refine 更新它,或 action=supersede 发起替代链。"
                ),
                failure_kind="validation",
                rendered=f"[memory_write denied] duplicate of {hit_path}",
            )
        frontmatter: dict[str, Any] = {
            "title": title,
            "status": "candidate",
            "evidence": evidence,
            "created": time.time(),
        }
        body = f"# {title}\n\n{content}\n"
        await self._write_and_reindex(rel, _dump_entry(frontmatter, body))
        return ToolResult(
            ok=True,
            content={"path": rel, "action": "create"},
            rendered=f"[memory_write ok] created {rel}",
        )

    async def _refine(self, rel: str, content: str) -> ToolResult:
        path = self._tree.root / rel
        if not path.exists():
            return ToolResult(
                ok=False, content=None,
                error=f"refine 目标不存在: {rel}(用 action=create 新建)",
                failure_kind="not_found",
                rendered=f"[memory_write failed] {rel} not found",
            )
        fm, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
        title = str(fm.get("title") or Path(rel).stem)
        fm["updated"] = time.time()
        body = f"# {title}\n\n{content}\n"
        await self._write_and_reindex(rel, _dump_entry(fm, body))
        return ToolResult(
            ok=True,
            content={"path": rel, "action": "refine"},
            rendered=f"[memory_write ok] refined {rel}",
        )

    async def _supersede(
        self, rel: str, title: str, content: str, evidence: str, invocation: ToolInvocation,
    ) -> ToolResult:
        raw_old = str(invocation.input.get("supersedes") or "").strip()
        old_rel = self._normalize_path(raw_old)
        old_path = self._tree.root / old_rel
        if not old_path.exists():
            return ToolResult(
                ok=False, content=None,
                error=f"supersede 目标不存在: {old_rel}",
                failure_kind="not_found",
                rendered=f"[memory_write failed] {old_rel} not found",
            )
        new_fm: dict[str, Any] = {
            "title": title,
            "status": "candidate",
            "supersedes": old_rel,
            "evidence": evidence,
            "created": time.time(),
        }
        body = f"# {title}\n\n{content}\n"
        await self._write_and_reindex(rel, _dump_entry(new_fm, body))

        old_fm, old_body = _split_frontmatter(old_path.read_text(encoding="utf-8"))
        old_fm["status"] = "deprecated"
        old_fm["superseded_by"] = rel
        old_fm["updated"] = time.time()
        old_path.write_text(_dump_entry(old_fm, old_body), encoding="utf-8")
        await self._tree.reindex_paths((old_path,))
        return ToolResult(
            ok=True,
            content={"path": rel, "action": "supersede", "superseded": old_rel},
            rendered=f"[memory_write ok] superseded {old_rel} → {rel}",
        )

    async def _find_duplicate(self, rel: str, title: str, content: str) -> tuple[float, str] | None:
        query_text = f"{title}\n{content[:500]}"
        query_tokens = _token_set(query_text)
        if not query_tokens:
            return None
        hits = await self._engine.search(SearchQuery(text=query_text, top_k=5))
        best = 0.0
        best_path: str | None = None
        for hit in hits:
            node = hit.node
            if node is None or node.path == rel:
                continue
            haystack = _token_set(f"{node.title}\n{node.body}")
            if not haystack:
                continue
            sim = len(query_tokens & haystack) / len(query_tokens)
            if sim > best:
                best, best_path = sim, node.path
        if best_path is not None and best >= self._threshold:
            return (best, best_path)
        return None

    async def _write_and_reindex(self, rel: str, text: str) -> None:
        path = self._tree.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        await self._tree.reindex_paths((path,))

    # ---- plumbing ---------------------------------------------------------

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
