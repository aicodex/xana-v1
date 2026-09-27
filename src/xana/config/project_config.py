"""项目配置分层(docs/design/project-design.md §4)。

用户级 {XANA_HOME}/config/system.json 放全局配置;项目级 projects/<id>/config.json 只能放
PROJECT_KEYS(机器人、daemon 端口 / socket、渠道→会话映射、工具禁用)。加载时用项目配置覆盖用户配置的
这些键;项目配置出现其他键即报错——模型、凭据、搜索服务地址、策略不能由项目改。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from xana.config.paths import project_home, xana_home
from xana.config.system_model import DaemonConfig, SystemConfig

PROJECT_KEYS = ("channels", "daemon", "sessions", "tools")
PROJECT_CONFIG_NAME = "config.json"


class ProjectConfigError(ValueError):
    pass


def user_system_json() -> Path:
    return xana_home() / "config" / "system.json"


def project_config_path(pid: str | None = None) -> Path:
    return project_home(pid) / PROJECT_CONFIG_NAME


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def read_project_config(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    extra = sorted(set(data) - set(PROJECT_KEYS))
    if extra:
        raise ProjectConfigError(f"{path} 含项目配置不允许的键 {extra};只能有 {list(PROJECT_KEYS)}")
    return data


def merge_system_config(user: dict[str, Any], project: dict[str, Any]) -> SystemConfig:
    return SystemConfig(**{**user, **project})


def load_system_config(pid: str | None = None) -> SystemConfig:
    """同步读取合并后的配置(xanac、setup 等不经 boot 的入口用)。"""
    return merge_system_config(_read_json(user_system_json()), read_project_config(project_config_path(pid)))


def update_project_config(path: Path, **changes: Any) -> None:
    """原子地改写项目配置的若干键(chmod 600,内含机器人凭据)。值与磁盘一致时不写。"""
    bad = sorted(set(changes) - set(PROJECT_KEYS))
    if bad:
        raise ProjectConfigError(f"项目配置不允许的键 {bad}")
    path = Path(path)
    data = read_project_config(path)
    new = {**data, **changes}
    if new == data and path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(new, indent=2, ensure_ascii=False))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_project_sessions(path: Path, system: SystemConfig) -> None:
    """SessionResolver 的镜像写:只写渠道→会话映射到项目配置。"""
    update_project_config(path, sessions=dict(system.sessions))


def daemon_socket(daemon: DaemonConfig, pid: str | None = None) -> Path:
    """unix socket 路径:配置了就用配置的,否则每个项目各自一个。"""
    if daemon.unix_socket:
        return Path(daemon.unix_socket).expanduser()
    return project_home(pid) / "run" / "serve.sock"
