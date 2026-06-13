"""Memory contracts.

Three sub-systems live here, sharing a tree-of-nodes substrate:

1. **ActiveContext / HistoryMirror** — runtime conversation context + durable mirror.
2. **TreeMem** — design-time knowledge tree (nodes, atomic ops, lint).
   yuxu treemem and CC's CLAUDE.md/skills/rules all land here.
3. **Search + Injection** — shared search backend (keyword + embedding, no LLM),
   consumed by two layers:
     - L1 Injector: pushed into prompt by PromptAssembler (HarnessSDK side).
     - L2 Query Tool: pulled by the model via a registered Tool (Tool side).
   Both call the same MemorySearchEngine; they package results differently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

HistoryKind = Literal["user", "assistant", "tool", "system", "compact"]
MemoryNodeKind = Literal["dir", "doc", "leaf"]
LintViolationKind = Literal[
    "size_exceeded",
    "type_mismatch",
    "anchor_invalid",
    "sibling_similar",
    "parent_child_count",
    "description_missing",
    "frontmatter_invalid",
]
LintSeverity = Literal["error", "warn", "info"]
InjectionPlacement = Literal["system_head", "system_tail", "user_prefix"]
SearchBackendKind = Literal["keyword", "embedding", "hybrid", "external"]
PlaceholderStatus = Literal["running", "completed", "cancelled", "failed"]


@dataclass(frozen=True)
class MemoryMessage:
    role: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActiveContext:
    session_id: str
    agent_name: str
    messages: tuple[MemoryMessage, ...] = ()
    summary: str = ""
    pinned_facts: tuple[str, ...] = ()
    token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoryRow:
    row_id: str
    session_id: str
    agent_name: str
    kind: HistoryKind
    payload: MemoryMessage
    ts: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplayResult:
    loaded_sessions: int
    loaded_rows: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Placeholder:
    """会话消息序列中的占位符：标记「这里有一个 run，状态见 status」。

    与 MemoryMessage 平级，由 ActiveContextStore.snapshot() 返回为混合序列
    `tuple[MemoryMessage | Placeholder, ...]`。**不**塞进 ActiveContext.messages
    （那里只放 MemoryMessage，保持类型纯净）。
    """

    run_id: str
    label: str
    status: PlaceholderStatus = "running"
    jsonl_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# TreeMem: persistent knowledge-tree nodes + lint findings.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryNode:
    """One node in the knowledge tree.

    `path` is the canonical address ("L2/feature/foo"). `parent_path` is the
    enclosing dir-node; root nodes have `parent_path == ""`. `body` is the
    rendered text used both for prompt injection and search indexing.
    """

    path: str
    kind: MemoryNodeKind
    title: str
    body: str
    parent_path: str = ""
    anchors: tuple[str, ...] = ()
    children_paths: tuple[str, ...] = ()
    frontmatter: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    updated_ts: float = 0.0


@dataclass(frozen=True)
class MemoryLintViolation:
    node_path: str
    kind: LintViolationKind
    severity: LintSeverity
    message: str
    current_value: Any = None
    limit_value: Any = None


@dataclass(frozen=True)
class MemoryLintResult:
    clean: bool
    violations: tuple[MemoryLintViolation, ...] = ()


# ---------------------------------------------------------------------------
# Shared search substrate.
# Both L1 (Injector) and L2 (Query Tool) call MemorySearchEngine; backends
# pluggable (BM25 / embedding / hybrid / ES). Query input shape is unified.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchQuery:
    """Canonical search input. Backends tokenize / embed `text` as needed.

    `scope_paths` restricts to subtree prefixes (e.g. ("L2/feature/",)).
    Pre-computed embeddings can be added as an optional field later
    without breaking existing callers.
    """

    text: str
    scope_paths: tuple[str, ...] = ()
    top_k: int = 10
    min_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchHit:
    """Backend-uniform result. Score is higher-is-better, normalized 0..1.

    `matched_spans` are short excerpts (backend-defined boundaries).
    `node` is optionally attached so callers don't need a second fetch.
    """

    node_path: str
    score: float
    matched_spans: tuple[str, ...] = ()
    node: MemoryNode | None = None


# ---------------------------------------------------------------------------
# L1: Injection contracts. PromptAssembler turns blocks into prompt segments.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InjectedBlock:
    """Ready-to-paste prompt block. Content is already size-budgeted.

    `reason` records why this block was selected; "always_on" / "path_match:cwd"
    / "keyword_match:auth". `score` is forwarded from the underlying SearchHit
    when applicable, otherwise None.
    """

    node_path: str
    content: str
    placement: InjectionPlacement
    reason: str
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
