"""Pydantic models for ~/.xana/config/system.json.

Schema mirrors yuxu_backup/src/yuxu/config_store.py but drops the strict
KNOWN_MODELS validator on ModelEntry.type — xana v1 accepts any provider
type string and validates at client-construction time. All other shapes
(extra="forbid" on top-level / extra="allow" on channels) follow the
yuxu_backup convention so a user's system.json is roundtrip-portable.

This is the "Python model" that constrains config content. It is NOT a
contract — it sits inside the config implementation and is consumed by
the loader (validation), the DSv4 client (model lookup), and any future
component that needs typed access. xana.config.contracts.RawConfig /
RuntimeConfig remain the public, frozen-dataclass surface.
"""
from __future__ import annotations

from typing import Literal, Optional

from xana.multimodal.contracts import InputModality
from xana.config.contracts import DEFAULT_CACHE_TTL_SECONDS

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    account: str = "default"
    api_key: str = ""
    base_url: Optional[str] = None
    api: Optional[str] = None
    budget_rmb: Optional[float] = None
    budget_kind: Optional[str] = None
    compact_override: Optional[int] = None  # 最终自动压缩触发值，直接覆盖推导结果。
    context_window: Optional[int] = Field(default=None, gt=0)  # 原始模型窗口，不能填派生触发值。
    max_tokens: Optional[int] = Field(default=None, gt=0)  # 请求输出上限，包含思考；未配置走预算兜底。
    # 该模型/端点的实际 prompt cache 保留期（秒）；本地策略依据，不是远程 TTL 开关。
    cache_ttl_seconds: int = Field(default=DEFAULT_CACHE_TTL_SECONDS, gt=0)
    input_modalities: list[InputModality] = []  # 显式开启 image / video；缺省仅文本
    fallback_to: Optional[str] = None


class DefaultModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat: Optional[str] = None
    agent: Optional[str] = None


class ModelTiers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fast: Optional[str] = None
    standard: Optional[str] = None
    deep: Optional[str] = None


class ProviderLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_concurrent: int = 2
    rpm: Optional[int] = None


class TelegramChannel(BaseModel):
    model_config = ConfigDict(extra="allow")
    bot_token: str = ""
    allowed_user_ids: list[int] = []


class FeishuChannel(BaseModel):
    model_config = ConfigDict(extra="allow")
    app_id: str = ""
    app_secret_key: str = ""
    domain: str = "feishu"


class Channels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    telegram: TelegramChannel = TelegramChannel()
    feishu: FeishuChannel = FeishuChannel()


class MemoryConfig(BaseModel):
    """记忆召回(docs/design/memory-recall-design.md §3.3):select = 模型挑选(默认,照 Claude Code)/
    lexical = 只用字面检索 / off = 只注入索引与置顶记忆。"""
    model_config = ConfigDict(extra="forbid")
    recall: Literal["select", "lexical", "off"] = "select"
    select_timeout_s: float = 3.0
    # 后台提取漏记的记忆:每 N 轮一次(按会话计),0 = 关闭(docs/design/memory-dream-design.md §3.1)
    extract_every: int = 1


class WebSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brave_search_key: str = ""
    tavily_api_key: str = ""
    bing_search_key: str = ""
    searxng_url: str = ""
    xana_search_url: str = ""    # 独立搜索服务(~/project/xana-search);为空则不注册 web_research/deep_research
    xana_search_token: str = ""


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 系统级禁用:组名或工具名;被禁用的工具不注册进工具目录,对所有 agent 都不存在
    disabled: list[str] = []


class AgentDefaults(BaseModel):
    """项目给 agent 的默认配置(docs/design/project-design.md §8):内置 chat 与没写 tools / skills 的
    AGENT.md 按此解析。写法同 AgentSpec 的工具 / 技能选择。"""
    model_config = ConfigDict(extra="forbid")
    tools: list[str] = ["default"]
    disallowed_tools: list[str] = []
    skills: list[str] | None = None     # None = 全部技能可见


class McpServer(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    transport: str = "stdio"


class Mcp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    servers: list[McpServer] = []

    def server(self, name: str) -> Optional[McpServer]:
        return next((s for s in self.servers if s.name == name), None)


class DaemonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unix_socket: str = ""               # 空 = 每个项目各自的 projects/<id>/run/serve.sock(project_config.daemon_socket)
    # Always bind a TCP port — this is the OS-level single-instance lock.
    # A second `xana serve` will fail with EADDRINUSE before it can take over
    # the unix socket (UnixSite silently unlinks the old one, defeating that
    # path as a guard). Empty/None values from legacy system.json auto-upgrade
    # to the default 127.0.0.1 below.
    bind: str = "127.0.0.1"
    port: int = 17777
    token: str = ""                    # 32-byte hex; setup wizard populates

    @field_validator("bind", mode="before")
    @classmethod
    def _coerce_bind(cls, v):
        if v is None or v == "":
            return "127.0.0.1"
        return v


class SystemConfig(BaseModel):
    """合并后的配置:用户级 ~/.xana/config/system.json + 项目级 projects/<id>/config.json
    (channels / daemon / sessions / tools 由项目覆盖,见 config/project_config.py)。

    ``sessions`` is the unified (adapter_id, thread_id) -> session_id map
    used by SessionResolver. Key format: ``f"{adapter_id}:{thread_id}"``
    where thread_id is feishu/tg chat_id, "" for cli. See
    docs/design/session-and-card-state.md §1.0. The file is read ONLY at
    boot; runtime mirrors writes here but never re-reads (per I16).
    """
    model_config = ConfigDict(extra="forbid")

    models: list[ModelEntry] = []
    default_model: DefaultModel = DefaultModel()
    model_tiers: ModelTiers = ModelTiers()
    limits: dict[str, ProviderLimit] = {}
    channels: Channels = Channels()
    web_search: WebSearch = WebSearch()
    memory: MemoryConfig = MemoryConfig()
    tools: ToolsConfig = ToolsConfig()
    agent_defaults: AgentDefaults = AgentDefaults()
    mcp: Mcp = Mcp()
    daemon: DaemonConfig = DaemonConfig()
    sessions: dict[str, str] = {}

    def model(self, name: str) -> Optional[ModelEntry]:
        return next((m for m in self.models if m.name == name), None)

    def limit(self, provider: str) -> ProviderLimit:
        return self.limits.get(provider) or ProviderLimit()
