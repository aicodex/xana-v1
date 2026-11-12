"""Versioned capability-package format; never stores model or channel credentials."""
from __future__ import annotations

import re
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentPackage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format_version: int = Field(default=1, ge=1, le=1)
    name: str = Field(pattern=r'^[a-z][a-z0-9_-]{1,39}$')
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    description: str = Field(min_length=1)
    entry_agent: str = 'chat'
    agents: list[str] = []
    skills: dict[str, list[str]] = {}  # agent -> workspace-relative SKILL.md paths
    agent_defaults: dict = {}
    python_dependencies: list[str] = []
    executables: list[str] = []
    settings: dict[str, str] = {}  # environment variable -> recipient-facing prompt

    @model_validator(mode='after')
    def valid_runtime(self):
        if self.runtime_url and (not self.runtime_url.startswith('https://') or not re.fullmatch(r'[a-f0-9]{64}',self.runtime_sha256)):
            raise ValueError('远程运行核必须提供 HTTPS URL 和 SHA-256')
        return self

    @field_validator('python_dependencies')
    @classmethod
    def valid_dependencies(cls, value):
        if any(not item.strip() or item.startswith('-') or '\n' in item for item in value):
            raise ValueError('Python 依赖必须是包要求，不能是 pip 选项')
        return value

    @field_validator('settings')
    @classmethod
    def valid_settings(cls, value):
        reserved={'HOME','PATH','PYTHONPATH','XANA_HOME','XANA_PROJECT','CODEX_HOME'}
        if any(not re.fullmatch(r'[A-Z_][A-Z0-9_]*',name) or name in reserved for name in value):
            raise ValueError('业务配置不能覆盖运行环境或目录变量')
        return value

    runtime_version: str
    runtime_url: str | None = None
    runtime_sha256: str = ""
    files: dict[str, str] = {}  # workspace-relative path -> sha256


MANIFEST = 'xana-agent.json'
