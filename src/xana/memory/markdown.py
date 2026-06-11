"""Concrete markdown-backed memory implementation.

This implements the existing MemoryTree / MemorySearchEngine / MemoryInjector
interfaces. It mirrors the useful Claude Code behavior: an entry markdown file,
bounded @include expansion, frontmatter headers, markdown/wiki links, and a
rebuildable SQLite FTS index.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from xana.memory.contracts import (
    InjectedBlock,
    MemoryLintResult,
    MemoryLintViolation,
    MemoryNode,
    SearchHit,
    SearchQuery,
)
from xana.memory.interfaces import MemoryInjector, MemorySearchEngine, MemoryTree

MAX_INCLUDE_DEPTH = 5
FRONTMATTER_MAX_LINES = 30
TEXT_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
}

_INCLUDE_RE = re.compile(r"(?<!`)@([^\s`]+(?:\\ [^\s`]+)*)")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-/\.]+")
_FTS5_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)


class MarkdownMemoryTree(MemoryTree):
    """MemoryTree backed by markdown files under one root directory.

    The markdown files are the source of truth. The in-memory node map is a
    rebuilt view over entry links, frontmatter references, markdown links,
    wikilinks, and bounded @include edges.
    """

    def __init__(self, root: str | Path, entrypoint: str = "README_ai.md") -> None:
        self.root = Path(root).resolve()
        self.entrypoint = entrypoint
        self.nodes: dict[str, MemoryNode] = {}
        self._edge_sources: dict[tuple[str, str], str] = {}

    async def load(self) -> None:
        """Rebuild the tree view from markdown files on disk.

        Behavior:
          * if the entrypoint (README_ai.md by default) is absent, return an
            empty tree without touching the filesystem — otherwise running
            xanac from a non-project directory (HOME, /, etc.) would `rglob`
            the entire tree and hang on the user's whole home dir.
          * otherwise walk the tree but skip hidden dirs (.git, .xana, .venv,
            …) and a handful of common cache dirs so a repo with a node_modules
            checkout or a .git pack doesn't dominate load time.
        """
        self.nodes = {}
        self._edge_sources = {}
        entry = self.root / self.entrypoint
        if not entry.exists():
            return
        self._load_file(entry, depth=0, stack=())
        for path in sorted(self._iter_md_files(self.root)):
            self._load_file(path, depth=0, stack=())
        self._attach_children_from_edges()

    def _iter_md_files(self, root: Path):
        """Yield *.md files under root, skipping hidden + cache dirs.

        Equivalent to ``root.rglob('*.md')`` but excludes any directory whose
        name starts with '.' (.git, .venv, .xana, .yuxu …) or matches a known
        cache dirname. We DO NOT prune by file size, content, or symlink —
        only by directory name, which is enough to keep load fast on a typical
        dev tree.
        """
        skip_dirnames = {"__pycache__", "node_modules", "dist", "build", ".pytest_cache"}
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                name = entry.name
                if entry.is_dir():
                    if name.startswith(".") or name in skip_dirnames:
                        continue
                    stack.append(entry)
                elif entry.is_file() and entry.suffix == ".md":
                    yield entry

    async def get_node(self, path: str) -> MemoryNode | None:
        return self.nodes.get(path)

    async def list_children(self, parent_path: str) -> tuple[MemoryNode, ...]:
        parent = self.nodes.get(parent_path)
        if parent is None:
            return ()
        return tuple(self.nodes[p] for p in parent.children_paths if p in self.nodes)

    async def lint(self, root_path: str = "") -> MemoryLintResult:
        violations: list[MemoryLintViolation] = []
        for node_path, node in self.nodes.items():
            if root_path and not node_path.startswith(root_path):
                continue
            for target in node.children_paths:
                if target not in self.nodes:
                    violations.append(
                        MemoryLintViolation(
                            node_path=node_path,
                            kind="anchor_invalid",
                            severity="error",
                            message=f"dangling reference: {target}",
                        )
                    )
            if not node.title.strip():
                violations.append(
                    MemoryLintViolation(
                        node_path=node_path,
                        kind="description_missing",
                        severity="warn",
                        message="title is empty",
                    )
                )
        return MemoryLintResult(clean=not violations, violations=tuple(violations))

    async def split_node(self, path: str, children: tuple[MemoryNode, ...], index_body: str) -> MemoryNode:
        snapshot = dict(self.nodes)
        try:
            current = self.nodes[path]
            for child in children:
                if child.path in self.nodes:
                    raise ValueError(f"child path already exists: {child.path}")
                self.nodes[child.path] = replace(child, parent_path=path)
            updated = replace(
                current,
                kind="dir",
                body=index_body,
                children_paths=current.children_paths + tuple(c.path for c in children),
                version=current.version + 1,
                updated_ts=time.time(),
            )
            self.nodes[path] = updated
            return updated
        except Exception:
            self.nodes = snapshot
            raise

    async def merge_nodes(self, survivor_path: str, absorbed_path: str, merged_body: str) -> MemoryNode:
        snapshot = dict(self.nodes)
        try:
            survivor = self.nodes[survivor_path]
            absorbed = self.nodes[absorbed_path]
            if absorbed.children_paths:
                raise ValueError("cannot merge a node with children")
            updated = replace(survivor, body=merged_body, version=survivor.version + 1, updated_ts=time.time())
            self.nodes[survivor_path] = updated
            del self.nodes[absorbed_path]
            if absorbed.parent_path in self.nodes:
                parent = self.nodes[absorbed.parent_path]
                self.nodes[absorbed.parent_path] = replace(
                    parent,
                    children_paths=tuple(p for p in parent.children_paths if p != absorbed_path),
                )
            return updated
        except Exception:
            self.nodes = snapshot
            raise

    async def move_node(self, path: str, new_parent_path: str) -> MemoryNode:
        snapshot = dict(self.nodes)
        try:
            node = self.nodes[path]
            if new_parent_path and new_parent_path not in self.nodes:
                raise ValueError(f"missing parent: {new_parent_path}")
            parent_base = _node_child_base(new_parent_path) if new_parent_path else ""
            new_path = f"{parent_base}/{Path(path).name}" if parent_base else Path(path).name
            if new_path != path and new_path in self.nodes:
                raise ValueError(f"target path already exists: {new_path}")
            moved = replace(node, path=new_path, parent_path=new_parent_path, version=node.version + 1, updated_ts=time.time())
            del self.nodes[path]
            self.nodes[new_path] = moved
            if node.parent_path in self.nodes:
                old_parent = self.nodes[node.parent_path]
                self.nodes[node.parent_path] = replace(
                    old_parent,
                    children_paths=tuple(p for p in old_parent.children_paths if p != path),
                )
            if new_parent_path:
                parent = self.nodes[new_parent_path]
                self.nodes[new_parent_path] = replace(parent, children_paths=parent.children_paths + (new_path,))
            return moved
        except Exception:
            self.nodes = snapshot
            raise

    async def redefine_parent(self, parent_path: str, new_index_body: str) -> MemoryNode:
        parent = self.nodes[parent_path]
        updated = replace(parent, body=new_index_body, version=parent.version + 1, updated_ts=time.time())
        self.nodes[parent_path] = updated
        return updated

    async def delete_node(self, path: str, reason: str) -> None:
        snapshot = dict(self.nodes)
        try:
            node = self.nodes[path]
            if node.children_paths:
                raise ValueError("cannot delete node with children")
            del self.nodes[path]
            if node.parent_path in self.nodes:
                parent = self.nodes[node.parent_path]
                self.nodes[node.parent_path] = replace(
                    parent,
                    children_paths=tuple(p for p in parent.children_paths if p != path),
                )
        except Exception:
            self.nodes = snapshot
            raise

    async def rename_node(self, path: str, new_name: str) -> MemoryNode:
        snapshot = dict(self.nodes)
        try:
            node = self.nodes[path]
            suffix = new_name if new_name.endswith(".md") else f"{new_name}.md"
            current_parent = Path(path).parent.as_posix()
            new_path = f"{current_parent}/{suffix}" if current_parent != "." else suffix
            if new_path != path and new_path in self.nodes:
                raise ValueError(f"target path already exists: {new_path}")
            renamed = replace(node, path=new_path, title=new_name.removesuffix(".md"), version=node.version + 1)
            del self.nodes[path]
            self.nodes[new_path] = renamed
            if node.parent_path in self.nodes:
                parent = self.nodes[node.parent_path]
                self.nodes[node.parent_path] = replace(
                    parent,
                    children_paths=tuple(new_path if p == path else p for p in parent.children_paths),
                )
            return renamed
        except Exception:
            self.nodes = snapshot
            raise

    def edge_source(self, source: str, target: str) -> str | None:
        return self._edge_sources.get((source, target))

    def _load_file(self, path: Path, depth: int, stack: tuple[Path, ...]) -> str | None:
        resolved = path.resolve()
        if not self._is_under_root(resolved) or resolved.suffix not in TEXT_EXTENSIONS:
            return None
        if resolved in stack:
            return None
        if depth > MAX_INCLUDE_DEPTH:
            return None
        if not resolved.exists() or not resolved.is_file():
            return None

        rel = self._rel(resolved)
        text = resolved.read_text(encoding="utf-8")
        frontmatter, body = _split_frontmatter(text)
        includes = self._extract_includes(body, resolved)
        included_bodies: list[str] = []
        for include_path in includes:
            include_rel = self._load_file(include_path, depth + 1, stack + (resolved,))
            if include_rel:
                self._edge_sources[(rel, include_rel)] = "at_include"
                included = self.nodes.get(include_rel)
                if included is not None:
                    included_bodies.append(included.body)

        anchors = tuple(m.group(2).strip() for m in _HEADING_RE.finditer(body))
        rendered_body = body.strip()
        if included_bodies:
            rendered_body = "\n\n".join([rendered_body, *included_bodies]).strip()
        title = str(frontmatter.get("title") or frontmatter.get("name") or _title_from_markdown(body) or Path(rel).stem)
        kind = "dir" if rel == self.entrypoint or frontmatter.get("type") == "index" else "doc"
        node = MemoryNode(
            path=rel,
            kind=kind,
            title=title,
            body=rendered_body,
            parent_path="",
            anchors=anchors,
            frontmatter=frontmatter,
            updated_ts=resolved.stat().st_mtime,
        )
        self.nodes[rel] = node
        for target, source in self._extract_reference_targets(rel, body, frontmatter):
            self._edge_sources[(rel, target)] = source
        return rel

    def _attach_children_from_edges(self) -> None:
        children: dict[str, list[str]] = {path: [] for path in self.nodes}
        parent_by_child: dict[str, str] = {}
        for (source, target), edge_source in self._edge_sources.items():
            if source not in self.nodes or target not in self.nodes:
                continue
            if target not in children[source]:
                children[source].append(target)
            if edge_source in {"frontmatter", "markdown_link", "wikilink", "at_include"} and target not in parent_by_child:
                parent_by_child[target] = source
        for path, node in list(self.nodes.items()):
            self.nodes[path] = replace(
                node,
                parent_path=parent_by_child.get(path, ""),
                children_paths=tuple(children.get(path, ())),
            )

    def _extract_includes(self, body: str, source_path: Path) -> tuple[Path, ...]:
        paths: list[Path] = []
        in_fence = False
        in_comment = False
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if line.startswith("```") or line.startswith("~~~"):
                in_fence = not in_fence
                continue
            if "<!--" in line:
                in_comment = True
            if in_fence or in_comment:
                if "-->" in line:
                    in_comment = False
                continue
            if line.startswith("@include "):
                target = self._resolve_ref(source_path, line.removeprefix("@include ").strip(), relative=True)
                if target is not None:
                    paths.append(target)
                continue
            for match in _INCLUDE_RE.finditer(raw_line):
                ref = match.group(1).replace("\\ ", " ").split("#", 1)[0]
                target = self._resolve_ref(source_path, ref, relative=True)
                if target is not None:
                    paths.append(target)
        return tuple(paths)

    def _extract_reference_targets(self, source_rel: str, body: str, frontmatter: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        source_path = self.root / source_rel
        out: list[tuple[str, str]] = []
        refs = frontmatter.get("references", ())
        if isinstance(refs, list):
            for ref in refs:
                ref_path = ref.get("path") if isinstance(ref, dict) else ref
                target = self._resolve_ref(source_path, str(ref_path)) if ref_path else None
                if target is not None:
                    out.append((self._rel(target), "frontmatter"))
        for match in _MARKDOWN_LINK_RE.finditer(body):
            ref = match.group(1).split("#", 1)[0]
            target = self._resolve_ref(source_path, ref, relative=True)
            if target is not None:
                out.append((self._rel(target), "markdown_link"))
        for match in _WIKI_LINK_RE.finditer(body):
            ref = match.group(1).split("|", 1)[0].split("#", 1)[0]
            target = self._resolve_ref(source_path, ref, relative=False)
            if target is not None:
                out.append((self._rel(target), "wikilink"))
        return tuple(out)

    def _resolve_ref(self, source_path: Path, ref: str, *, relative: bool = False) -> Path | None:
        ref = ref.strip()
        if not ref or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", ref):
            return None
        if ref.startswith("~/"):
            target = Path.home() / ref[2:]
        elif ref.startswith("/"):
            target = Path(ref)
        elif relative or ref.startswith("./") or ref.startswith("../"):
            target = source_path.parent / ref
        else:
            target = self.root / ref
        if target.suffix == "":
            target = target.with_suffix(".md")
        target = target.resolve()
        if not self._is_under_root(target):
            return None
        if target.suffix not in TEXT_EXTENSIONS:
            return None
        return target

    def _is_under_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()


class SQLiteMemorySearchEngine(MemorySearchEngine):
    """SQLite-backed MemorySearchEngine using FTS5 when available."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.conn = sqlite3.connect(str(db_path))
        self.nodes: dict[str, MemoryNode] = {}
        self._has_fts = self._init_schema()

    async def index(self, node: MemoryNode) -> None:
        self.nodes[node.path] = node
        with self.conn:
            self.conn.execute(
                "replace into memory_nodes(path, title, body, frontmatter) values (?, ?, ?, ?)",
                (node.path, node.title, node.body, repr(node.frontmatter)),
            )
            if self._has_fts:
                self.conn.execute("delete from memory_fts where path = ?", (node.path,))
                self.conn.execute(
                    "insert into memory_fts(path, title, body) values (?, ?, ?)",
                    (node.path, node.title, node.body),
                )

    async def remove(self, node_path: str) -> None:
        self.nodes.pop(node_path, None)
        with self.conn:
            self.conn.execute("delete from memory_nodes where path = ?", (node_path,))
            if self._has_fts:
                self.conn.execute("delete from memory_fts where path = ?", (node_path,))

    async def search(self, query: SearchQuery) -> tuple[SearchHit, ...]:
        if not query.text.strip():
            return ()
        if self._has_fts:
            hits = self._search_fts(query)
        else:
            hits = self._search_like(query)
        return tuple(h for h in hits if h.score >= query.min_score)[: query.top_k]

    async def rebuild_from_tree(self, tree: MarkdownMemoryTree) -> None:
        with self.conn:
            self.conn.execute("delete from memory_nodes")
            if self._has_fts:
                self.conn.execute("delete from memory_fts")
        self.nodes = {}
        for node in tree.nodes.values():
            await self.index(node)

    def _init_schema(self) -> bool:
        with self.conn:
            self.conn.execute(
                "create table if not exists memory_nodes(path text primary key, title text not null, body text not null, frontmatter text not null)"
            )
            try:
                self.conn.execute("create virtual table if not exists memory_fts using fts5(path unindexed, title, body)")
                return True
            except sqlite3.OperationalError:
                return False

    def _search_fts(self, query: SearchQuery) -> list[SearchHit]:
        # FTS5 bareword grammar errors on punctuation (`/`, `-`, `.`, `~`, …):
        # collapse them to spaces before tokenizing so a query like
        # `~/.xana/code` becomes `xana OR code`, not a parser-error.
        cleaned = _FTS5_PUNCT_RE.sub(" ", query.text)
        q = " OR ".join(_TOKEN_RE.findall(cleaned))
        if not q:
            return []
        rows = self.conn.execute(
            "select path, bm25(memory_fts) as rank, snippet(memory_fts, 2, '[', ']', ' ... ', 12) from memory_fts where memory_fts match ? order by rank limit ?",
            (q, max(query.top_k * 4, query.top_k)),
        ).fetchall()
        hits: list[SearchHit] = []
        for path, rank, snippet in rows:
            if query.scope_paths and not any(path.startswith(scope) for scope in query.scope_paths):
                continue
            node = self.nodes.get(path)
            score = 1.0 / (1.0 + max(float(rank), 0.0))
            hits.append(SearchHit(node_path=path, score=score, matched_spans=(snippet,), node=node))
        return hits

    def _search_like(self, query: SearchQuery) -> list[SearchHit]:
        tokens = [t.lower() for t in _TOKEN_RE.findall(query.text)]
        hits: list[SearchHit] = []
        for path, node in self.nodes.items():
            if query.scope_paths and not any(path.startswith(scope) for scope in query.scope_paths):
                continue
            haystack = f"{node.title}\n{node.body}".lower()
            overlap = [t for t in tokens if t in haystack]
            if not overlap:
                continue
            score = len(set(overlap)) / max(len(set(tokens)), 1)
            spans = tuple(line for line in node.body.splitlines() if any(t in line.lower() for t in overlap))[:2]
            hits.append(SearchHit(node_path=path, score=score, matched_spans=spans, node=node))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits


class SearchMemoryInjector(MemoryInjector):
    """MemoryInjector implementation backed by an existing search engine."""

    def __init__(self, engine: MemorySearchEngine, top_k: int = 5, budget_chars: int = 4000) -> None:
        self.engine = engine
        self.top_k = top_k
        self.budget_chars = budget_chars

    async def name(self) -> str:
        return "memory_search"

    async def select(self, session_id: str, agent_name: str, user_input: str, cwd: str) -> tuple[InjectedBlock, ...]:
        hits = await self.engine.search(SearchQuery(text=user_input, top_k=self.top_k))
        blocks: list[InjectedBlock] = []
        remaining = self.budget_chars
        for hit in hits:
            node = hit.node
            if node is None or remaining <= 0:
                continue
            content = node.body[:remaining]
            remaining -= len(content)
            blocks.append(
                InjectedBlock(
                    node_path=hit.node_path,
                    content=content,
                    placement="system_tail",
                    reason="memory_search",
                    score=hit.score,
                    metadata={"session_id": session_id, "agent_name": agent_name, "cwd": cwd},
                )
            )
        return tuple(blocks)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for idx, line in enumerate(lines[1:FRONTMATTER_MAX_LINES], start=1):
        if line.strip() == "---":
            end = idx
            break
    if end is None:
        return {}, text
    return _parse_simple_yaml(lines[1:end]), "\n".join(lines[end + 1 :])


def _parse_simple_yaml(lines: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    current_key: str | None = None
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and current_key:
            out.setdefault(current_key, []).append(_parse_scalar(line[4:].strip()))
            continue
        if line.startswith("- ") and current_key:
            out.setdefault(current_key, []).append(_parse_scalar(line[2:].strip()))
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            current_key = key
            if value == "":
                out[key] = []
            else:
                out[key] = _parse_scalar(value)
    return out


def _parse_scalar(value: str) -> Any:
    value = value.strip().strip('"').strip("'")
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return value


def _title_from_markdown(body: str) -> str | None:
    match = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    return match.group(1).strip() if match else None


def _node_child_base(node_path: str) -> str:
    if node_path.endswith(".md"):
        return node_path.removesuffix(".md")
    return node_path
