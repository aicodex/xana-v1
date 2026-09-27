"""xana sync — refresh ~/.xana/code/ and ~/.xana/mem/ from the xana source.

This is the install-time / dev-iteration plumbing: every launcher in
~/.xana/bin loads xana from ~/.xana/code/, so the code that runs is whatever
the *last* `xana sync` wrote — never the developer's live src/ checkout.
`pip install -U xana` followed by `xana sync` updates the runtime; nothing
imports the source repo at runtime.

Source resolution order:

  1. ``--source`` flag (a repo root or a xana package dir);
  2. ``~/.xana/.sync_source`` sidecar — the path written by the previous
     successful sync, used when ``xana sync`` runs from the launcher itself
     (where ``xana.__file__`` would point at the *target*, not the source);
  3. ``Path(xana.__file__).parent`` — works for the first install when xana
     is on PYTHONPATH or pip-installed.

Behavior is **wipe + copy**, not merge, so renamed / removed files cannot
leak in stale form. The sync refuses to run when the resolved source lies
inside the target tree — that would be self-deletion (commit ede6d54 left
a stale ``~/.xana/code/xana`` exactly this way).
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import TextIO

import xana

from xana.config.paths import xana_home


_CODE_SUBDIR = "code"
_MEM_SUBDIR = "mem"
_SOURCE_SIDECAR = ".sync_source"
_MEM_TOP_LEVEL_FILES = ("README_ai.md",)
_MEM_TOP_LEVEL_DIRS = ("doc_ai", "exp_ai", "todo_ai")
_IGNORE_PATTERNS = ("__pycache__", "*.pyc", "*.pyo", "*.pyd")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="xana package dir or repo root (default: sidecar from last sync, then xana.__file__)",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        default=False,
        help="do not restart a running `xana serve` after sync",
    )


def run(args: argparse.Namespace, *, out: TextIO) -> int:
    home = xana_home()
    restart = not getattr(args, "no_restart", False)
    # 先停掉所有在运行的项目 serve(之后可能要迁移数据,不能有进程在写),同步完再逐个拉起
    stopped = _stop_running_serves(home, out) if restart else []
    code_target = home / _CODE_SUBDIR / "xana"
    mem_target = home / _MEM_SUBDIR
    sidecar = home / _SOURCE_SIDECAR

    code_source = _resolve_code_source(args.source, sidecar, out)
    if code_source is None:
        return 1
    if _is_within(code_source, code_target.parent) or code_source == code_target:
        print(
            f"[xana sync] refusing self-overwrite: source {code_source} lies inside "
            f"target {code_target.parent}",
            file=out,
        )
        print("           pass --source <repo-or-package-path> pointing at the original source", file=out)
        return 1

    print(f"[xana sync] code source         {code_source}", file=out)
    print(f"[xana sync] code target         {code_target}", file=out)
    code_count = _wipe_and_copytree(code_source, code_target)
    print(f"  written {code_count} files", file=out)

    repo_root = _discover_repo_root(code_source)
    if repo_root is not None:
        print(f"[xana sync] mem source          {repo_root}", file=out)
        print(f"[xana sync] mem target          {mem_target}", file=out)
        mem_count = _sync_mem(repo_root, mem_target)
        print(f"  written {mem_count} files", file=out)
    else:
        print("[xana sync] mem source NOT FOUND — running from a wheel? skipping mem sync.", file=out)

    home.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(str(code_source) + "\n", encoding="utf-8")

    if restart or not _any_serve_running(home):
        from xana.cli.commands.project import MigrationError, ensure_default_project
        try:
            ensure_default_project(out=out)
        except MigrationError as exc:
            print(f"[xana sync] 迁移失败,未启动 serve:{exc}", file=out)
            return 1
    else:
        print("[xana sync] serve 仍在运行(--no-restart),跳过项目初始化 / 迁移", file=out)
    for pid in stopped:
        _start_serve(home, pid, out)
    return 0


def serve_pid_paths(home: Path) -> dict[str, Path]:
    """各项目 serve 的 pidfile;旧布局(迁移前)的 ~/.xana/run/serve.pid 记为默认项目。"""
    paths = {}
    base = home / "projects"
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if (d / "run" / "serve.pid").exists():
                paths[d.name] = d / "run" / "serve.pid"
    legacy = home / "run" / "serve.pid"
    if legacy.exists():
        paths.setdefault("default", legacy)
    return paths


def _live_pid(pidfile: Path) -> int | None:
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) and _cmdline_looks_like_serve(pid) else None


def _any_serve_running(home: Path) -> bool:
    return any(_live_pid(p) for p in serve_pid_paths(home).values())


def _stop_running_serves(home: Path, out: TextIO) -> list[str]:
    """停止所有在运行的项目 serve,返回它们的项目 id(同步后按原样重启)。"""
    stopped = []
    for project, pidfile in serve_pid_paths(home).items():
        pid = _live_pid(pidfile)
        if pid is None:
            pidfile.unlink(missing_ok=True)
            continue
        _terminate(pid)
        pidfile.unlink(missing_ok=True)
        stopped.append(project)
        print(f"[xana sync] stopped serve of project {project} (pid {pid})", file=out)
    if not stopped:
        print("[xana sync] serve not running — nothing to restart", file=out)
    return stopped


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _cmdline_looks_like_serve(pid: int) -> bool:
    """Best-effort check that <pid> is a xana serve process (Linux /proc only)."""
    proc = Path(f"/proc/{pid}/cmdline")
    try:
        data = proc.read_bytes()
    except OSError:
        return True  # non-Linux or unreadable: skip the check gracefully
    return b"serve" in data


def _terminate(pid: int, *, timeout: float = 5.0, poll: float = 0.2) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(poll)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _start_serve(home: Path, project: str, out: TextIO) -> None:
    log_path = home / "projects" / project / "logs" / "serve.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as logfile:
        proc = subprocess.Popen(
            [str(home / "bin" / "xana"), "serve", "--project", project],
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"[xana sync] serve of project {project} restarted (pid {proc.pid}, log {log_path})", file=out)


def _resolve_code_source(
    flag: Path | None, sidecar: Path, out: TextIO
) -> Path | None:
    """Pick the xana package dir to copy from, in order: --source > sidecar > xana.__file__."""
    if flag is not None:
        path = flag.expanduser().resolve()
        normalized = _normalize_package_dir(path)
        if normalized is None:
            print(f"[xana sync] --source {path} does not contain a xana package", file=out)
            return None
        return normalized
    if sidecar.exists():
        recorded = Path(sidecar.read_text(encoding="utf-8").strip()).expanduser()
        if recorded.is_dir() and (recorded / "__init__.py").exists():
            return recorded.resolve()
        print(
            f"[xana sync] sidecar source {recorded} is missing — pass --source explicitly",
            file=out,
        )
        return None
    return Path(xana.__file__).resolve().parent


def _normalize_package_dir(path: Path) -> Path | None:
    """Accept a repo root (src/xana/, xana/) or an actual package dir."""
    candidates = (
        path / "src" / "xana",
        path / "xana",
        path,
    )
    for c in candidates:
        if (c / "__init__.py").exists():
            return c.resolve()
    return None


def _discover_repo_root(code_source: Path) -> Path | None:
    """Walk up from the xana package dir until pyproject.toml is found."""
    for ancestor in code_source.parents:
        if (ancestor / "pyproject.toml").exists():
            return ancestor
    return None


def _wipe_and_copytree(src: Path, dst: Path) -> int:
    _wipe(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*_IGNORE_PATTERNS))
    return sum(1 for p in dst.rglob("*") if p.is_file())


def _sync_mem(repo_root: Path, mem_target: Path) -> int:
    _wipe(mem_target)
    mem_target.mkdir(parents=True, exist_ok=True)
    written = 0
    for name in _MEM_TOP_LEVEL_FILES:
        src = repo_root / name
        if src.is_file():
            shutil.copy2(src, mem_target / name)
            written += 1
    for name in _MEM_TOP_LEVEL_DIRS:
        src = repo_root / name
        if src.is_dir():
            shutil.copytree(
                src, mem_target / name, ignore=shutil.ignore_patterns(*_IGNORE_PATTERNS)
            )
            written += sum(1 for p in (mem_target / name).rglob("*") if p.is_file())
    return written


def _wipe(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
