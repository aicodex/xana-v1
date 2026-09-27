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
import socket
import time
from pathlib import Path
from typing import TextIO

from xana.config.paths import DEFAULT_PROJECT, PROJECT_FILE, list_projects, project_home, project_root, xana_home
from xana.config.project_config import (
    PROJECT_KEYS,
    load_system_config,
    project_config_path,
    update_project_config,
    user_system_json,
)
from xana.config.system_model import SystemConfig

WORKSPACE_SEED = "# xana 助手\n\n你是 xana,一个通过聊天渠道服务用户的个人助手。\n"
# 旧布局里属于"当前唯一项目"的数据:~/.xana/<name> → projects/default/<name>
LEGACY_ITEMS = ("workspace", "sessions", "runs", "run", "channels", "scheduled_tasks.json", "evolution", "worktrees")
LEGACY_LOGS = ("serve.log", "serve_stderr.log", "xana_debug_actctx.log", "xana_debug_compact.log")
LEGACY_UNIX_SOCKET = "~/.xana/run/serve.sock"
USER_KEYS_KEPT = ("tools",)   # tools 在用户级也可以有(全局禁用),不从 system.json 挪走
DEFAULT_PORT = 17777


class MigrationError(RuntimeError):
    pass


def _register(pid: str, root: Path) -> None:
    home = project_home(pid)
    home.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    readme = root / "README_ai.md"
    if not readme.exists():
        readme.write_text(WORKSPACE_SEED, encoding="utf-8")
    (home / PROJECT_FILE).write_text(json.dumps({"root": str(root)}, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")   # 最后写:它存在即表示项目就绪


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
    _register(DEFAULT_PROJECT, root)
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
    moving = {k: data[k] for k in PROJECT_KEYS if k in data and k not in USER_KEYS_KEPT}
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


def _free_port(start: int) -> int:
    used = set()
    for pid in list_projects():
        try:
            used.add(load_system_config(pid).daemon.port)
        except (OSError, ValueError):
            pass
    port = start
    while port in used or not _port_free(port):
        port += 1
    return port


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _running_pid(pid: str) -> int | None:
    try:
        n = int((project_home(pid) / "run" / "serve.pid").read_text(encoding="utf-8").strip())
        os.kill(n, 0)
        return n
    except (OSError, ValueError):
        return None


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="project_command", required=True)
    create = sub.add_parser("create", help="注册一个新项目")
    create.add_argument("id")
    create.add_argument("--root", type=Path, required=True, help="项目目录(记忆树与项目级 agents / skills)")
    create.add_argument("--port", type=int, default=None, help="daemon 端口;缺省自动分配")
    sub.add_parser("list", help="列出项目")


def run(args: argparse.Namespace, *, out: TextIO) -> int:
    if args.project_command == "list":
        for pid in list_projects():
            try:
                port = load_system_config(pid).daemon.port
            except (OSError, ValueError):
                port = "?"
            running = _running_pid(pid)
            state = f"运行中(pid {running})" if running else "未运行"
            print(f"{pid:<16} {state:<18} 端口 {port:<6} {project_root(pid)}", file=out)
        return 0
    pid = args.id
    try:
        home = project_home(pid)
    except ValueError as exc:
        print(f"[xana project] {exc}", file=out)
        return 1
    if (home / PROJECT_FILE).exists():
        print(f"[xana project] 项目 {pid} 已存在:{project_root(pid)}", file=out)
        return 1
    port = args.port or _free_port(DEFAULT_PORT + 1)
    update_project_config(project_config_path(pid),
                          daemon={"unix_socket": "", "bind": "127.0.0.1", "port": port,
                                  "token": secrets.token_hex(32)})
    root = args.root.expanduser().resolve()
    _register(pid, root)
    print(f"[xana project] 已创建项目 {pid}:目录 {root},端口 {port}", file=out)
    print(f"  机器人等渠道配置写在 {project_config_path(pid)} 的 channels;启动:xana serve --project {pid}",
          file=out)
    return 0
