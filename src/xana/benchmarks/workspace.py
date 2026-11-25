"""Prepare a trial's private runtime state from an explicitly selected model."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from xana.config.system_model import SystemConfig


def prepare_home(source: Path, target: Path, *, model: str, tools: list[str],
                 system_prompt: str | None = None, max_turns: int = 200,
                 portable: bool = False) -> None:
    """Copy only the selected model, never channels, schedules, memory or agents.

    ``source`` must be a dedicated benchmark login/configuration directory.
    Local workers share the dedicated login authority (including its file lock).
    Containers receive access tokens only; they must never rotate a copied refresh token.
    """
    if max_turns < 1:
        raise ValueError("max_turns must be positive")
    if target.exists():
        raise FileExistsError(target)
    config = SystemConfig.model_validate_json((source / "config/system.json").read_text())
    entry = config.model(model)
    if entry is None:
        raise ValueError(f"Model {model!r} is not configured in {source}")
    if entry.type.startswith("kimi") and not (source / "credentials/kimi.json").is_file():
        raise ValueError("Benchmark Kimi login is missing")
    target.mkdir(parents=True, mode=0o700)
    config_dir = target / "config"
    config_dir.mkdir(mode=0o700)
    minimal = {
        "models": [entry.model_dump(exclude_none=True, exclude={"fallback_to"})],
        "default_model": {"chat": model, "agent": model},
        "agent_defaults": {"tools": [], "skills": []},
    }
    config_file = config_dir / "system.json"
    config_file.write_text(json.dumps(minimal), encoding="utf-8")
    config_file.chmod(0o600)
    if entry.type.startswith("kimi"):
        credentials = target / "credentials"
        if portable:
            credentials.mkdir(mode=0o700)
            token = json.loads((source / "credentials/kimi.json").read_text())
            token["refresh_token"] = ""
            (credentials / "kimi.json").write_text(json.dumps(token))
            (credentials / "kimi.json").chmod(0o600)
        else:
            credentials.symlink_to((source / "credentials").resolve(), target_is_directory=True)
    agent_dir = target / "agents/benchmark"
    agent_dir.mkdir(parents=True)
    front = {
        "kind": "agent", "name": "benchmark", "goal": "Official benchmark trial",
        "tools": tools, "skills": [], "max_turns": max_turns,
        "allow_exec": "Bash" in tools, "allow_exec_reason": "Isolated benchmark container",
    }
    # Existing Xana template for TB; external benchmark policy verbatim for tau.
    body = system_prompt if system_prompt is not None else "${base_prompt}\n${env}"
    (agent_dir / "AGENT.md").write_text(
        "---\n" + yaml.safe_dump(front) + "---\n" + body, encoding="utf-8"
    )


def remove_credentials(home: Path) -> None:
    """Remove only adapter-created trial secrets, preserving execution evidence."""
    (home / "config/system.json").unlink(missing_ok=True)
    if (home / "credentials").is_symlink():
        (home / "credentials").unlink()
    else:
        (home / "credentials/kimi.json").unlink(missing_ok=True)
