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
from typing import Any, Awaitable, Callable, Literal

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
# 注入块的去向(harness 按它分成不同的 ContextNote):index = 记忆索引,pinned = 置顶记忆全文,
# recall = 本条消息召回的记忆全文(docs/design/memory-recall-design.md §3.2–3.3)
InjectionPlacement = Literal["index", "pinned", "recall"]
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


# ---------------------------------------------------------------------------
# 项目记忆(memory/ 目录,docs/design/memory-recall-design.md)
# ---------------------------------------------------------------------------

MemoryType = Literal["user", "feedback", "project", "reference"]
MEMORY_TYPES: tuple[MemoryType, ...] = ("user", "feedback", "project", "reference")


@dataclass(frozen=True)
class MemoryRecord:
    """一条记忆:memory/<slug>.md。path 相对项目根目录;mtime 用于"写于 N 天前"的过时提示。"""
    slug: str
    path: str
    name: str
    description: str
    type: MemoryType
    pinned: bool
    body: str
    mtime: float
    topic: str = ""          # 可选主题(如 估值 / 产业链 / 选股),索引按 类型 → 主题 分组


@dataclass(frozen=True)
class ExtractionRequest:
    """一轮结束后交给后台记忆提取的内容(docs/design/memory-dream-design.md §3.1)。
    messages = 本轮的用户消息起到最终回答为止;interactive = 用户在线的聊天会话(否则是无人值守运行)。"""
    session_id: str
    agent_name: str
    run_id: str
    messages: "tuple[MemoryMessage, ...]"
    interactive: bool


# 后台记忆提取:harness 在一轮完成后以后台任务调用,实现由 runtime 注入(调用模型)。
MemoryExtractor = Callable[[ExtractionRequest], Awaitable[None]]


# 召回选择:输入(用户这句话, 候选记忆),返回选中的 slug(按相关度)。实现可调用模型(由 runtime 注入)。
MemorySelector = Callable[[str, "tuple[MemoryRecord, ...]"], Awaitable["tuple[str, ...]"]]
