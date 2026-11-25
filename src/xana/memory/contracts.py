"""Memory contracts.

Two sub-systems live here:

1. **ActiveContext / HistoryMirror** — runtime conversation context + durable mirror.
2. **Project Memory + SAG2 Retrieval/Injection** — authoritative `memory/**/*.md`
   files with a shared `MemorySearchEngine` (SAG2) consumed by both:
     - L1 Injector (`ProjectMemoryInjector`): pushed into prompt at turn start.
     - L2 Query Tool (`memory_search`): pulled by the model on demand.
"""
from __future__ import annotations

from xana.multimodal.contracts import MediaInput

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

HistoryKind = Literal["user", "assistant", "tool", "system", "compact"]
MemoryNodeKind = Literal["dir", "doc", "leaf"]
# 注入块的去向(harness 按它分成不同的 ContextNote):index = 记忆索引,pinned = 置顶记忆全文,
# recall = 本条消息经 SAG2 召回的记忆全文
InjectionPlacement = Literal["index", "pinned", "recall"]
MEMORY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MemoryMessage:
    role: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    media: tuple[MediaInput, ...] = ()


@dataclass(frozen=True)
class ActiveContext:
    session_id: str
    agent_name: str
    messages: tuple[MemoryMessage, ...] = ()
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












# ---------------------------------------------------------------------------
# Search document view attached to SearchHit.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryNode:
    """One memory document view attached to search hits."""

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
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceCandidate:
    id: str
    source_path: str
    source_hash: str
    title: str
    content: str
    spans: tuple[tuple[int, int], ...] = ()
    previous_score: float | None = None


@dataclass(frozen=True)
class EvidenceScore:
    candidate_id: str
    score: float


@dataclass(frozen=True)
class EvidenceJudgment:
    scores: tuple[EvidenceScore, ...]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 1


EvidenceJudge = Callable[[str, tuple[EvidenceCandidate, ...], tuple[EvidenceCandidate, ...]],
                         Awaitable[EvidenceJudgment]]


@dataclass(frozen=True)
class EvidenceCriterion:
    """Host-approved model instructions; no backend invents a scoring prompt."""
    question: str
    when_true: str
    when_false: str
    source: str
    score_note: str = ""

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.question, self.when_true, self.when_false, self.source)):
            raise ValueError("evidence criterion and its source are required")


@dataclass(frozen=True)
class SAG2Options:
    """Reference retrieval parameters; the host supplies its query deadline."""
    candidate_k: int = 100
    seed_k: int = 10
    max_hops: int = 2
    expansion_term_limit: int = 20

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in
               (self.candidate_k, self.seed_k, self.expansion_term_limit)):
            raise ValueError("SAG2 counts must be positive integers")
        if type(self.max_hops) is not int or not 0 <= self.max_hops <= 2:
            raise ValueError("SAG2 supports zero to two expansion rounds")


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


# ---------------------------------------------------------------------------
# Stage 5 RSI & standalone contracts (JSON-serializable, frozen).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemorySnapshotEntry:
    """Single memory file captured inside a MemorySnapshot."""

    slug: str
    sha256: str
    raw_text: str


@dataclass(frozen=True)
class MemorySnapshot:
    """Point-in-time snapshot of a MemoryStore for deterministic rollback & fork."""

    snapshot_id: str
    created_ts: float
    entries: tuple[MemorySnapshotEntry, ...] = ()
    schema_version: int = MEMORY_SCHEMA_VERSION


@dataclass(frozen=True)
class RetrievalContext:
    """Input to a swappable MemoryRetrievalPolicy."""

    session_id: str
    agent_name: str
    user_input: str
    cwd: str
    records: tuple[MemoryRecord, ...] = ()
    mode: str = "sag2"
    max_recall: int = 5
    body_char_limit: int = 2000
    total_char_limit: int = 6000
    min_terms: int = 2
    run_id: str = ""
    schema_version: int = MEMORY_SCHEMA_VERSION


@dataclass(frozen=True)
class RetrievalPlan:
    """Output of a MemoryRetrievalPolicy: budgeted blocks + diagnostics."""

    blocks: tuple[InjectedBlock, ...] = ()
    recalled_paths: tuple[str, ...] = ()
    skipped_reason: str = ""
    latency_ms: float = 0.0
    judge_models: tuple[str, ...] = ()
    schema_version: int = MEMORY_SCHEMA_VERSION


@dataclass(frozen=True)
class MemoryWriteOutcome:
    """Structured result of a memory write/append/delete operation."""

    action: Literal["create", "update", "append", "delete", "skip"]
    path: str
    ok: bool
    reason: str = ""
    schema_version: int = MEMORY_SCHEMA_VERSION


@dataclass(frozen=True)
class ExtractionOutcome:
    """Structured result of background memory extraction."""

    session_id: str
    run_id: str
    written_paths: tuple[str, ...] = ()
    skipped_reason: str = ""
    latency_ms: float = 0.0
    error: str = ""
    schema_version: int = MEMORY_SCHEMA_VERSION
