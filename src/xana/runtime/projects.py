"""项目管理(docs/design/project-design.md §5、§7):命令行 `xana project` 与聊天里的 project_manage 共用。

一个项目 = projects/<id>/(私有:配置、会话、日志、锁)+ 项目目录(记忆树、项目级 agents / skills)。
一个项目一个 serve 进程;这里负责注册、查询、配置与启停,不涉及 serve 内部。
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xana.config.paths import DEFAULT_PROJECT, PROJECT_FILE, list_projects, project_home, project_root, xana_home
from xana.config.project_config import load_system_config, project_config_path, read_project_config, \
    update_project_config

INSTRUCTIONS_FILE = "AGENTS.md"          # 项目说明:harness 每个会话读项目根目录的这个文件
DEFAULT_INSTRUCTIONS = """\
# default 项目

这是 xana 的默认项目,也是项目的管理台:日常聊天、资料检索与研究,以及创建 / 配置其他项目。

- 写代码、改代码、调试之前先加载 coding 技能。
"""
DEFAULT_PORT = 17777


@dataclass(frozen=True)
class ProjectInfo:
    id: str
    root: Path
    port: int
    running_pid: int | None
    feishu_bot: str | None          # 已接的飞书机器人名(或 app_id);None = 未接
    agent_defaults: dict[str, Any]


def register(pid: str, root: Path) -> None:
    """写 project.json(最后写:它存在即表示项目就绪)。"""
    home = project_home(pid)
    home.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    (home / PROJECT_FILE).write_text(json.dumps({"root": str(root)}, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")


def write_instructions(root: Path, text: str) -> bool:
    """项目根目录还没有 AGENTS.md 时写入;已有则不覆盖,返回 False。"""
    path = root / INSTRUCTIONS_FILE
    if path.exists():
        return False
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return True


def create_project(pid: str, *, root: Path | None = None, port: int | None = None,
                   agent_defaults: dict[str, Any] | None = None) -> ProjectInfo:
    """注册新项目。root 缺省为 projects/<id>/workspace;端口缺省分配一个空闲的。已存在抛 FileExistsError。"""
    home = project_home(pid)                       # 同时校验 id 格式(ValueError)
    if (home / PROJECT_FILE).exists():
        raise FileExistsError(f"项目 {pid} 已存在:{project_root(pid)}")
    root = (root.expanduser().resolve() if root is not None else home / "workspace")
    changes: dict[str, Any] = {"daemon": {"unix_socket": "", "bind": "127.0.0.1",
                                          "port": port or free_port(DEFAULT_PORT + 1),
                                          "token": secrets.token_hex(32)}}
    if agent_defaults:
        changes["agent_defaults"] = agent_defaults
    update_project_config(project_config_path(pid), **changes)
    register(pid, root)
    return project_info(pid)


def project_info(pid: str) -> ProjectInfo:
    cfg = load_system_config(pid)
    feishu = cfg.channels.feishu
    bot = ((feishu.model_extra or {}).get("bot_name") or feishu.app_id) if feishu is not None and feishu.app_id else None
    raw = read_project_config(project_config_path(pid))
    return ProjectInfo(id=pid, root=project_root(pid), port=cfg.daemon.port, running_pid=running_pid(pid),
                       feishu_bot=bot, agent_defaults=dict(raw.get("agent_defaults") or {}))


def all_projects() -> list[ProjectInfo]:
    return [project_info(pid) for pid in list_projects()]


def set_agent_defaults(pid: str, agent_defaults: dict[str, Any]) -> None:
    project_root(pid)                              # 未注册抛 LookupError
    update_project_config(project_config_path(pid), agent_defaults=agent_defaults)


def set_feishu_bot(pid: str, creds: dict[str, Any]) -> None:
    """写入扫码注册得到的飞书机器人,并把确认人(本应用下的 open_id)加入该项目的配对白名单。"""
    from xana.agent_adapter.channels._pairing import PairingStore

    project_root(pid)
    raw = read_project_config(project_config_path(pid))
    channels = dict(raw.get("channels") or {})
    channels["feishu"] = {
        "app_id": creds["app_id"], "app_secret_key": creds["app_secret"], "domain": creds.get("domain", "feishu"),
        "open_id": creds.get("open_id"), "bot_open_id": creds.get("bot_open_id"), "bot_name": creds.get("bot_name"),
    }
    update_project_config(project_config_path(pid), channels=channels)
    if creds.get("open_id"):
        PairingStore(path=project_home(pid) / "channels" / "pairings.json").allow(
            "feishu", creds["open_id"], note="扫码创建机器人的主人")


def serve_pidfile(pid: str) -> Path:
    """serve 拿到单实例锁后写入自己的 pid,退出时删除(cli/serve.py)。"""
    return project_home(pid) / "run" / "serve.pid"


def running_pid(pid: str) -> int | None:
    return live_serve_pid(serve_pidfile(pid))


def live_serve_pid(pidfile: Path) -> int | None:
    """pidfile 记录的进程仍存活且是 serve 时返回其 pid(含启动中的 serve);否则 None。"""
    try:
        n = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return n if _alive(n) and _looks_like_serve(n) else None


def _alive(n: int) -> bool:
    """进程是否存活。由本进程启动的 serve(start_project)退出后是僵尸进程,kill(0) 仍会成功,
    所以先用非阻塞 waitpid 回收自己的子进程。"""
    try:
        reaped, _status = os.waitpid(n, os.WNOHANG)
        if reaped == n:
            return False
    except ChildProcessError:
        pass                                       # 不是本进程的子进程
    try:
        os.kill(n, 0)
        return True
    except PermissionError:
        return True                                # 存在,只是不属于当前用户
    except OSError:
        return False


def _looks_like_serve(n: int) -> bool:
    """崩溃后 pid 可能被别的进程复用:按命令行确认是 serve(仅 Linux /proc,读不到时不拦)。"""
    try:
        return b"serve" in Path(f"/proc/{n}/cmdline").read_bytes()
    except OSError:
        return True


def spawn_serve(pid: str) -> int:
    """后台拉起该项目的 serve(输出追加到 logs/serve.log),返回进程 pid。不检查是否已在运行。"""
    log = project_home(pid) / "logs" / "serve.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    xana_bin = xana_home() / "bin" / "xana"
    with open(log, "ab") as fh:
        proc = subprocess.Popen([str(xana_bin) if xana_bin.exists() else "xana", "serve", "--project", pid],
                                stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    return proc.pid


def start_project(pid: str) -> int:
    """后台启动该项目的 serve,返回进程 pid;已在运行抛 RuntimeError。"""
    project_root(pid)
    if running_pid(pid):
        raise RuntimeError(f"项目 {pid} 已在运行(pid {running_pid(pid)})")
    return spawn_serve(pid)


def stop_project(pid: str, *, timeout: float = 10.0) -> bool:
    """停止该项目的 serve;不在运行返回 False。default 项目不能从自己内部停止(调用方负责拦截)。"""
    n = running_pid(pid)
    if n is None:
        return False
    terminate_serve(n, serve_pidfile(pid), timeout=timeout)
    return True


def terminate_serve(n: int, pidfile: Path, *, timeout: float = 10.0) -> None:
    """SIGTERM(serve 优雅收尾),超时 SIGKILL;之后删掉仍记录着它的 pidfile(被 KILL 的 serve 来不及自己删)。"""
    try:
        os.kill(n, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while _alive(n) and time.monotonic() < deadline:
        time.sleep(0.2)
    if _alive(n):
        try:
            os.kill(n, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _alive(n)                                  # 回收
    try:
        if pidfile.read_text(encoding="utf-8").strip() == str(n):
            pidfile.unlink()
    except OSError:
        pass


def free_port(start: int) -> int:
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


__all__ = ["DEFAULT_PROJECT", "ProjectInfo", "all_projects", "create_project", "live_serve_pid", "project_info",
           "register", "running_pid", "serve_pidfile", "set_agent_defaults", "set_feishu_bot", "spawn_serve",
           "start_project", "stop_project", "terminate_serve"]
