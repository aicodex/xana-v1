---
title: Modular RSI Interfaces Reference
type: doc
status: active
---

# Xana 模块化接口文档（Stage 0–7）

本文档汇总 Xana 模块化重构后的全部正式抽象接口（ABC）、可替换 RSI 策略槽、数据契约黄金锁、Tape 事件命名空间、诊断日志标签与独立运行入口。所有签名与 `src/xana/*/interfaces.py` 源码严格一致。

---

## 0. 全局约定

- **单向依赖**：所有业务包均可向下依赖 `xana.protocol`（纯 stdlib）；跨包依赖受 [test_arch_layers.py](../tests/test_arch_layers.py) AST 门禁锁定（只减不增）。
- **唯一组合根**：所有默认实现、策略槽与录制器统一在 [boot.py](../src/xana/runtime/boot.py) 的 `boot()` 中构造注入。
- **契约黄金锁**：每个模块的公开 wire/策略 dataclass 均锁定在 `tests/golden/<pkg>_contract_v1.json`，字段破坏性变更由 `tests/contract_guard.py` 自动拦截。
- **版本化策略槽**：所有可进化策略 ABC 均提供 `component_version() -> ComponentVersion`，便于 `RunManifest` 与 `LedgerEntry.manifest_hash` 追溯跨模块版本拼装。
- **诊断日志开关**：通过 `XANA_DIAG` 环境变量按标签开启（例如 `XANA_DIAG=llm,memory,harness` 或 `XANA_DIAG=all` 或 `XANA_DIAG=all:debug`），格式为 `[xana:<tag>:op|perf|anomaly|detail]`，自动脱敏。
- **Tape 录制开关**：`boot(..., tape_dir=Path(...))` 或设置环境变量 `XANA_TAPE_DIR=/path/to/dir`，即自动向 6 个子系统注入共享 `TapeWriter`；未开启时零开销 no-op。

---

## 1. Stage 0：协议与观测底座（`src/xana/protocol/`）

- **核心模块**：`primitives.py`、`versioning.py`、`tape.py`、`eval.py`、`diag.py`
- **关键原语**：`ComponentVersion`、`TokenUsage`、`ProtocolError`、`BlobRef`、`ContentPart`、`Provenance`
- **磁带与快照**：`TapeWriter`、`read_tape`、`RunManifest`、`TapeHeader`、`TapeEvent`、`EvalTrialRecord`、`BenchmarkSnapshot`、`build_benchmark_snapshot`
- **诊断日志**：`DiagLogger(tag)`（`.op()` / `.perf(..., latency_ms=, slow_ms=)` / `.anomaly()` / `.detail()` / `.span()`）、`configure_diag(spec)`、`diag_enabled(tag)`
- **黄金锁**：`tests/golden/protocol_contract_v1.json`
- **独立 CLI**：`uv run python -m xana.protocol tape inspect|verify|slice|diff ...`

---

## 2. Stage 1：LLM Driver（`src/xana/llm_driver/`）

- **源码**：`interfaces.py`、`contracts.py`、`driver.py`、`policies.py`、`tape_recorder.py`
- **诊断标签**：`XANA_DIAG=llm` | **Tape 事件**：`llm.request`、`llm.attempt`、`llm.outcome`
- **黄金锁**：`tests/golden/llm_driver_contract_v1.json` | **独立 CLI**：`uv run python -m xana.llm_driver bench`

### 正式 ABC

```python
class ModelClient(ABC):
    async def capabilities(self, model: str | None = None) -> ProviderCapabilities
    def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]
    async def cancel(self, request_id: str) -> None

class ModelRegistry(ABC):
    async def register(self, provider: str, client: ModelClient) -> None
    async def get(self, provider: str) -> ModelClient | None

class WireTransport(ABC):
    @property
    def dialect(self) -> WireDialect
    def capabilities(self, profile: ProviderProfile, model: str | None = None) -> ProviderCapabilities
    def stream_attempt(self, request: ModelRequest, profile: ProviderProfile, credentials: CredentialResolver) -> AsyncGenerator[ModelEvent, None]
    async def aclose(self) -> None

class CredentialResolver(ABC):
    @property
    def can_refresh(self) -> bool
    async def auth_headers(self, *, force_refresh: bool = False) -> dict[str, str]
```

### 可替换 RSI 策略槽

```python
class RetryPolicyStrategy(ABC):
    def component_version(self) -> ComponentVersion
    def decide(self, error: ProviderError, *, attempt: int, request: ModelRequest) -> RetryDecision

class StreamRecoveryStrategy(ABC):
    def component_version(self) -> ComponentVersion
    def decide(self, error: ProviderError, *, attempt: int, has_visible_output: bool, has_tool_calls: bool, request: ModelRequest) -> StreamRecoveryDecision

class RequestRewriteStrategy(ABC):
    def component_version(self) -> ComponentVersion
    def rewrite_on_error(self, request: ModelRequest, error: ProviderError, *, attempt: int) -> RewriteDecision | None

class RoutingStrategy(ABC):
    def component_version(self) -> ComponentVersion
    def select_target(self, request: ModelRequest, *, failed_targets: tuple[tuple[str, str], ...] = (), last_error: ProviderError | None = None) -> RouteTarget | None
```

---

## 3. Stage 2：Policy & 执行环境（`src/xana/policy/`）

- **源码**：`interfaces.py`、`contracts.py`、`approvals.py`、`path_policy.py`、`shell.py`、`exec_env.py`、`tape_recorder.py`
- **诊断标签**：`XANA_DIAG=policy` | **Tape 事件**：`policy.decision`、`env.exec`
- **黄金锁**：`tests/golden/policy_contract_v1.json` | **独立 CLI**：`uv run python -m xana.policy bench`

### 正式 ABC 与策略槽

```python
class PolicyStatePort(ABC):
    async def get_session_policy(self, session_id: str) -> SessionPolicySnapshot
    async def save_permission_mode(self, session_id: str, mode: PermissionMode) -> None
    async def add_permission_grant(self, session_id: str, fingerprint: str) -> None

class PolicyEventSink(ABC):
    def publish(self, kind: str, session_id: str, run_id: str, payload: dict[str, Any]) -> None

class CommandRiskClassifier(ABC):
    def component_version(self) -> ComponentVersion
    def classify(self, command: str) -> RiskAssessment

class PathBoundaryPolicy(ABC):
    def component_version(self) -> ComponentVersion
    def check_path(self, path: Path, *, operation: PathOperation, cwd: Path) -> PermissionResult

class ExecutionEnvironment(ABC):
    def component_version(self) -> ComponentVersion
    async def run_process(self, spec: ProcessSpec, *, abort: AbortLike | None = None) -> ProcessOutcome
    async def cancel(self, exec_id: str) -> None
```

---

## 4. Stage 3：Tools & 观测管理（`src/xana/tools/`）

- **源码**：`interfaces.py`、`contracts.py`、`batches.py`、`observation.py`、`disclosure.py`、`tape_recorder.py`
- **诊断标签**：`XANA_DIAG=tools` | **Tape 事件**：`tool.finished`
- **黄金锁**：`tests/golden/tools_contract_v1.json` | **独立 CLI**：`uv run python -m xana.tools bench`

### 正式 ABC 与策略槽

```python
class Tool(ABC):
    def spec(self) -> ToolSpec
    async def validate(self, invocation: ToolInvocation) -> None
    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult
    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation
    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult
    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult
    async def render_result(self, result: ToolResult) -> str
    async def cancel(self, call_id: str) -> None
    def temporal_for(self, invocation: ToolInvocation) -> ToolTemporal | None

class ToolRegistry(ABC):
    async def register(self, tool: Tool) -> None
    async def unregister(self, name: str) -> Tool | None
    async def get(self, name: str) -> Tool | None
    async def list_specs(self) -> tuple[ToolSpec, ...]
    async def register_toolset(self, toolset: Toolset) -> None
    async def list_toolsets(self) -> tuple[Toolset, ...]

class ToolEnvironment(ABC):
    async def execute(self, invocation: ToolInvocation, tool: Tool, context: ToolContext, run: Callable[[], Awaitable[ToolResult]]) -> ToolResult

class ObservationPolicy(ABC):
    def component_version(self) -> ComponentVersion
    def normalize(self, invocation: ToolInvocation, spec: ToolSpec, result: ToolResult, *, rendered: str) -> ToolObservation

class ToolDisclosurePolicy(ABC):
    def component_version(self) -> ComponentVersion
    def plan(self, specs: Sequence[ToolSpec], ctx: DisclosureContext, *, ext_specs: Sequence[ToolSpec] = ()) -> DisclosurePlan
```

---

## 5. Stage 4：Skills（`src/xana/skills/`）

- **源码**：`interfaces.py`、`contracts.py`、`catalog.py`、`proposals.py`、`tape_recorder.py`
- **诊断标签**：`XANA_DIAG=skills` | **Tape 事件**：`skill.catalog`、`skill.load`、`skill.proposal`
- **黄金锁**：`tests/golden/skills_contract_v1.json` | **独立 CLI**：`uv run python -m xana.skills bench`

### 正式 ABC 与策略槽

```python
class Skill(ABC):
    def spec(self) -> SkillSpec
    async def activate(self, activation: SkillActivation) -> SkillContribution
    async def deactivate(self, session_id: str) -> None

class SkillRegistry(ABC):
    async def register(self, skill: Skill) -> None
    async def get(self, name: str) -> Skill | None
    async def discover(self, activation: SkillActivation) -> tuple[Skill, ...]

class SkillLoader(ABC):
    async def load(self, source: str) -> Skill

class SkillManifestParser(ABC):
    async def parse(self, path: Path) -> SkillManifest

class SkillCatalogPolicy(ABC):
    def component_version(self) -> ComponentVersion
    def plan(self, specs: Sequence[SkillSpec], ctx: CatalogContext) -> CatalogPlan

class SkillProposalQueue(ABC):
    def submit(self, proposal: SkillProposal) -> SkillProposal
    def list(self, status: ProposalStatus | None = None) -> tuple[SkillProposal, ...]
    def get(self, proposal_id: str) -> SkillProposal | None
    def decide(self, proposal_id: str, *, status: Literal["approved", "rejected"], reason: str = "", now_ms: int | None = None) -> SkillProposal
    def prune_expired(self, now_ms: int | None = None) -> tuple[SkillProposal, ...]
```

---

## 6. Stage 5：Memory & SAG2（`src/xana/memory/`）

- **源码**：`interfaces.py`、`contracts.py`、`store.py`、`recall.py`、`project_search.py`、`tape_recorder.py`、`testing.py`
- **诊断标签**：`XANA_DIAG=memory` | **Tape 事件**：`memory.recall`、`memory.search`、`memory.write`、`memory.extract`、`memory.snapshot`
- **黄金锁**：`tests/golden/memory_contract_v1.json` | **独立 CLI**：`uv run python -m xana.memory bench`

### 长期记忆 ABC 与策略槽

```python
class MemoryStore(ABC):
    def list(self) -> tuple[MemoryRecord, ...]
    def get(self, slug: str) -> MemoryRecord | None
    def index_text(self, records: tuple[MemoryRecord, ...] | None = None) -> str
    def pinned_text(self, records: tuple[MemoryRecord, ...] | None = None) -> str
    def similar(self, name: str, description: str, *, exclude: str = "") -> tuple[MemoryRecord, float] | None
    def write(self, slug: str, *, name: str, description: str, mtype: str, body: str, pinned: bool = False, topic: str = "") -> MemoryRecord
    def append(self, slug: str, text: str) -> MemoryRecord
    def delete(self, slug: str) -> bool
    def snapshot(self) -> MemorySnapshot
    def fork(self, target_root: Path) -> MemoryStore
    def rollback(self, snapshot: MemorySnapshot) -> None

class MemorySearchEngine(ABC):
    async def index(self, node: MemoryNode) -> None
    async def remove(self, node_path: str) -> None
    async def search(self, query: SearchQuery) -> tuple[SearchHit, ...]

class MemoryRetrievalPolicy(ABC):
    def component_version(self) -> ComponentVersion
    async def plan(self, ctx: RetrievalContext) -> RetrievalPlan

class MemoryExtractionPolicy(ABC):
    def component_version(self) -> ComponentVersion
    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome

class MemoryInjector(ABC):
    async def name(self) -> str
    async def select(self, session_id: str, agent_name: str, user_input: str, cwd: str) -> tuple[InjectedBlock, ...]

class MemoryInjectorRegistry(ABC):
    async def register(self, injector: MemoryInjector, priority: int = 100) -> None
    async def list_active(self) -> tuple[MemoryInjector, ...]
    async def collect(self, session_id: str, agent_name: str, user_input: str, cwd: str) -> tuple[InjectedBlock, ...]
```

> 会话上下文与历史镜像 ABC（`ActiveContextStore`、`HistoryMirror`、`MemoryReplayer`）仍定义在 `xana.memory.interfaces` 中，配合 `ContextSDK` 运行。

---

## 7. Stage 6：Harness Core & Context SDK（`src/xana/harness_sdk/`、`src/xana/context/`）

- **源码**：`interfaces.py`、`contracts.py`、`policies.py`、`tape_recorder.py`、`default.py`、`context/sdk.py`
- **诊断标签**：`XANA_DIAG=harness`、`XANA_DIAG=context` | **Tape 事件**：`harness.compaction`、`harness.stop_decision`
- **黄金锁**：`tests/golden/harness_contract_v1.json` | **独立 CLI**：`uv run python -m xana.harness_sdk bench`

### 正式 ABC 与策略槽

```python
class Harness(ABC):
    async def compact(self, context: HarnessContext, *, on_progress: Callable[[str], Awaitable[None]] | None = None) -> CompactSummary | None
    async def cancel(self, session_id: str, run_id: str | None = None) -> bool
    def invoke(self, request: HarnessRequest, context: HarnessContext) -> AsyncGenerator[HarnessEvent, None]

class CompactionPolicy(ABC):
    def component_version(self) -> ComponentVersion
    def evaluate(self, ctx: CompactionContext) -> CompactionDecision

class StopDecisionPolicy(ABC):
    def component_version(self) -> ComponentVersion
    def decide_stop(self, ctx: StopContext) -> StopDecision
    def decide_silent_reminder(self, *, silent_calls: int, silent_reminders: int, is_child_run: bool) -> str | None

class ProgressSink(ABC):
    async def report(self, event: ProgressEvent) -> None

class UsageAccumulator(ABC):
    async def estimate_before_request(self, request: HarnessRequest, context: HarnessContext, messages: tuple[ModelMessage, ...]) -> UsageSnapshot
    async def apply_model_event(self, current: UsageSnapshot, event: ModelEvent) -> UsageSnapshot
```

---

## 8. Stage 7：Eval & Evolution（`src/xana/evolution/`）

- **源码**：`interfaces.py`、`contracts.py`、`promotion.py`、`ledger.py`、`evals.py`、`tape_recorder.py`
- **诊断标签**：`XANA_DIAG=eval` | **Tape 事件**：`eval.promotion`、`eval.ledger`
- **黄金锁**：`tests/golden/evolution_contract_v1.json` | **独立 CLI**：`uv run python -m xana.evolution bench`

### 正式 ABC 与策略槽

```python
class IterationLedgerStore(ABC):
    async def append(self, entry: LedgerEntry) -> None
    async def read_all(self) -> tuple[LedgerEntry, ...]
    async def recent(self, n: int) -> tuple[LedgerEntry, ...]
    def lint(self) -> list[LintViolation]

class PromotionGate(ABC):
    def component_version(self) -> ComponentVersion
    def evaluate(self, base_per_case: list[dict[str, Any]], cand_per_case: list[dict[str, Any]], *, criteria: PromotionCriteria) -> PromotionDecision

class BenchmarkRunner(ABC):
    def suite(self, name: str) -> Any
    async def run(self, suite: Any, cases: tuple[Any, ...], *, repeat: int = 1, agent: str | None = None, label: str = "", tape: tuple[str, ...] = (), on_case: Callable[[str], None] | None = None, reuse: bool = True) -> str
    async def score(self, suite: Any, batch: str, *, max_age_s: float = 21600.0) -> dict[str, Any]
    def batch_runs(self, suite: Any, batch: str) -> list[dict[str, Any]]
    def reusable_rows(self, suite: Any, agent: str) -> dict[str, list[dict[str, Any]]]
    def heldout_run_ids(self) -> frozenset[str]
```

---

## 9. 独立运行与诊断速查

```bash
# 运行各子系统独立离线 micro-benchmark（零宿主、零外部依赖）
uv run python -m xana.llm_driver bench
uv run python -m xana.policy bench
uv run python -m xana.tools bench
uv run python -m xana.skills bench
uv run python -m xana.memory bench
uv run python -m xana.harness_sdk bench
uv run python -m xana.evolution bench

# 开启按标签诊断日志（性能慢调用 / 异常 / 操作追踪）
XANA_DIAG=llm,memory,harness uv run xana serve --project demo
XANA_DIAG=all:debug uv run python -m xana.harness_sdk bench

# 开启统一 Stage 0 Tape 落盘
XANA_TAPE_DIR=/tmp/xana-tapes uv run xana serve --project demo
uv run python -m xana.protocol tape inspect /tmp/xana-tapes/<tape_id>
```
