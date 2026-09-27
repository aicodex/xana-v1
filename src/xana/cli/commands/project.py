"""xana project — 项目注册与默认项目(docs/design/project-design.md §5、§6)。

- ensure_default_project():创建默认项目 default(项目目录 ~/.xana/projects/default/workspace);
  发现旧布局(数据直接在 ~/.xana 下)时一次性迁移进默认项目。xana setup / xana sync 调用,幂等。
- xana project create <id> --root <目录> [--port N]:注册新项目(一个项目一个 serve,各自端口 / 机器人)。
- xana project list:列出项目、目录、端口、是否在运行。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import TextIO

from xana.config.paths import DEFAULT_PROJECT, PROJECT_FILE, project_home, xana_home
from xana.runtime.projects import all_projects, create_project, register
from xana.config.project_config import (
    PROJECT_ONLY_KEYS,
    load_system_config,
    project_config_path,
    update_project_config,
    user_system_json,
)
from xana.config.system_model import SystemConfig

# 旧布局里属于"当前唯一项目"的数据:~/.xana/<name> → projects/default/<name>
LEGACY_ITEMS = ("workspace", "sessions", "runs", "run", "channels", "scheduled_tasks.json", "evolution", "worktrees")
LEGACY_LOGS = ("serve.log", "serve_stderr.log", "xana_debug_actctx.log", "xana_debug_compact.log")
LEGACY_UNIX_SOCKET = "~/.xana/run/serve.sock"


class MigrationError(RuntimeError):
    pass


def ensure_default_project(*, out: TextIO) -> None:
    """默认项目不存在时创建;旧布局的数据先迁移进来(任何一步失败即抛 MigrationError,不写 project.json,
    重新运行会从中断处继续)。"""
    home = xana_home()
    dp = project_home(DEFAULT_PROJECT)
    if (dp / PROJECT_FILE).exists():
        return
    dp.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in LEGACY_ITEMS:
        src, dst = home / name, dp / name
        if not src.exists():
            continue
        if dst.exists():
            raise MigrationError(f"迁移冲突:{src} 与 {dst} 同时存在,请人工处理后重试")
        os.replace(src, dst)
        moved.append(name)
    for name in LEGACY_LOGS:
        src, dst = home / "logs" / name, dp / "logs" / name
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
            moved.append(f"logs/{name}")
    _split_system_json(out)
    _strip_session_config_keys(dp / "sessions", out)
    if moved:
        print(f"[xana project] 旧布局已迁移进默认项目 {dp}:{', '.join(moved)}", file=out)
    root = dp / "workspace"
    register(DEFAULT_PROJECT, root)
    cfg = load_system_config(DEFAULT_PROJECT)
    if not cfg.daemon.token:
        update_project_config(project_config_path(DEFAULT_PROJECT),
                              daemon={**cfg.daemon.model_dump(), "token": secrets.token_hex(32)})
    print(f"[xana project] 默认项目 default 就绪:{root}", file=out)


def _split_system_json(out: TextIO) -> None:
    """把 system.json 里属于项目的键(channels / daemon / sessions)挪到默认项目的 config.json。"""
    path = user_system_json()
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    moving = {k: data[k] for k in PROJECT_ONLY_KEYS if k in data}
    if not moving:
        return
    daemon = moving.get("daemon")
    if isinstance(daemon, dict) and daemon.get("unix_socket") == LEGACY_UNIX_SOCKET:
        daemon["unix_socket"] = ""          # 改为每个项目各自的 socket
    user_rest = {k: v for k, v in data.items() if k not in moving}
    SystemConfig(**{**user_rest, **moving})  # 拆分前先校验合并结果
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    update_project_config(project_config_path(DEFAULT_PROJECT), **moving)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(user_rest, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    path.chmod(0o600)
    print(f"[xana project] system.json 的 {sorted(moving)} 已移到默认项目配置(原文件备份 {backup.name})", file=out)


REMOVED_SESSION_CONFIG_KEYS = ("project_root",)   # SessionConfig 已删除的字段(project-design §5)


def _strip_session_config_keys(sessions: Path, out: TextIO) -> None:
    """会话配置文件(<sid>.json,含 session_id)里删掉已不存在的字段,否则按新契约加载会失败。"""
    if not sessions.is_dir():
        return
    fixed = 0
    for path in sessions.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "session_id" not in data:
            continue
        if any(k in data for k in REMOVED_SESSION_CONFIG_KEYS):
            for k in REMOVED_SESSION_CONFIG_KEYS:
                data.pop(k, None)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            fixed += 1
    if fixed:
        print(f"[xana project] {fixed} 个会话配置去掉了已废弃字段 {list(REMOVED_SESSION_CONFIG_KEYS)}", file=out)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="project_command", required=True)
    create = sub.add_parser("create", help="注册一个新项目")
    create.add_argument("id")
    create.add_argument("--root", type=Path, default=None,
                        help="项目目录(记忆树与项目级 agents / skills);缺省 ~/.xana/projects/<id>/workspace")
    create.add_argument("--port", type=int, default=None, help="daemon 端口;缺省自动分配")
    sub.add_parser("list", help="列出项目")


def run(args: argparse.Namespace, *, out: TextIO) -> int:
    if args.project_command == "list":
        for info in all_projects():
            state = f"运行中(pid {info.running_pid})" if info.running_pid else "未运行"
            bot = f"飞书 {info.feishu_bot}" if info.feishu_bot else "未接机器人"
            print(f"{info.id:<16} {state:<18} 端口 {info.port:<6} {bot:<20} {info.root}", file=out)
        return 0
    try:
        info = create_project(args.id, root=args.root, port=args.port)
    except (ValueError, FileExistsError) as exc:
        print(f"[xana project] {exc}", file=out)
        return 1
    print(f"[xana project] 已创建项目 {info.id}:目录 {info.root},端口 {info.port}", file=out)
    print(f"  接飞书机器人:在 default 项目里让 xana 帮你绑定(project_manage bind_feishu),"
          f"或编辑 {project_config_path(info.id)} 的 channels;启动:xana serve --project {info.id}", file=out)
    return 0
