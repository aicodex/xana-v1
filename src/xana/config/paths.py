"""xana 路径的唯一解析点(docs/design/project-design.md §3)。只依赖标准库,任何模块都可导入。

- xana_home():用户级目录,$XANA_HOME,缺省 ~/.xana(凭据、全局配置、用户级 agents / skills、安装)。
- project_id():当前进程所属项目,$XANA_PROJECT,缺省 default(`xana serve --project <id>` 设置)。
- project_home(id):项目私有数据 ~/.xana/projects/<id>(项目配置、会话、运行记录、日志、锁)。
- project_root(id):项目目录(记忆树与项目级 agents / skills),记录在 project_home/project.json;
  default 项目缺省为 project_home/workspace。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_PROJECT = "default"
PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
PROJECT_FILE = "project.json"


def xana_home() -> Path:
    override = os.environ.get("XANA_HOME")
    return Path(override).expanduser() if override else Path.home() / ".xana"


def project_id() -> str:
    return os.environ.get("XANA_PROJECT") or DEFAULT_PROJECT


def project_home(pid: str | None = None) -> Path:
    pid = pid or project_id()
    if not PROJECT_ID_RE.fullmatch(pid):
        raise ValueError(f"项目 id {pid!r} 不合法:小写字母或数字开头,只含小写字母、数字、-、_,至多 40 字符")
    return xana_home() / "projects" / pid


def project_root(pid: str | None = None) -> Path:
    """项目目录。未注册的项目抛 LookupError(default 项目未初始化时同样,需先运行 xana setup)。"""
    home = project_home(pid)
    meta = home / PROJECT_FILE
    if not meta.exists():
        raise LookupError(f"项目 {pid or project_id()!r} 未注册({meta} 不存在);默认项目请先运行 xana setup")
    root = json.loads(meta.read_text(encoding="utf-8")).get("root")
    if not root:
        raise LookupError(f"{meta} 缺少 root")
    return Path(root).expanduser()


def list_projects() -> list[str]:
    base = xana_home() / "projects"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if (p / PROJECT_FILE).exists())
