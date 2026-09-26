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

from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator


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
    compact_override: Optional[int] = None
    context_window: Optional[int] = None
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


class McpServer(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    transport: str = "stdio"


class Mcp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    servers: list[McpServer] = []

    def server(self, name: str) -> Optional[McpServer]:
        return next((s for s in self.servers if s.name == name), None)


_POLICY_MODES = ("default", "gated", "auto", "workspace_gated")


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_mode: str = "default"
    egress_tools: list[str] = ["web_browser"]
    secret_markers: list[str] = [
        "api_key", "api-key", "secret", "password", "passwd",
        "token", "private key", "begin rsa", "begin openssh",
    ]
    exfil_payload_bytes: int = 50_000

    @field_validator("default_mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        if v not in _POLICY_MODES:
            raise ValueError(
                f"unknown policy default_mode {v!r}; known: {list(_POLICY_MODES)}"
            )
        return v


class DaemonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unix_socket: str = "~/.xana/run/serve.sock"
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
    """System-level config persisted at ~/.xana/config/system.json.

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
    tools: ToolsConfig = ToolsConfig()
    mcp: Mcp = Mcp()
    policy: PolicyConfig = PolicyConfig()
    daemon: DaemonConfig = DaemonConfig()
    sessions: dict[str, str] = {}

    def model(self, name: str) -> Optional[ModelEntry]:
        return next((m for m in self.models if m.name == name), None)

    def limit(self, provider: str) -> ProviderLimit:
        return self.limits.get(provider) or ProviderLimit()
