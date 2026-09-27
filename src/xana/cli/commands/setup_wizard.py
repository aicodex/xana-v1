"""Interactive setup wizard: model entries + channel (Feishu QR / Telegram).

Mirrors yuxu_backup setup_wizard.py but minimal for xana v1:
  * model step  — one ModelEntry + default_model.chat/.agent
  * channel step — 1) Feishu scan-to-create  2) Telegram bot token  3) skip

Re-running `xana setup` always enters both steps; for an already-configured
section, pressing Enter at the first prompt skips it and keeps the existing
config (nothing is cleared). All answers flow through the injected `ask`
callable; tests drive it with canned answers.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Callable, Optional, TextIO

from xana.config.loader import ensure_user_system_json, user_config_dir
from xana.config.paths import project_id
from xana.config.project_config import (
    PROJECT_KEYS,
    PROJECT_ONLY_KEYS,
    daemon_socket,
    load_system_config,
    project_config_path,
    read_project_config,
    update_project_config,
)
from xana.config.system_model import SystemConfig

AskFn = Callable[[str], str]
FeishuRegisterFn = Callable[[], Optional[dict]]


def _load_raw() -> dict:
    """用户级 system.json 与当前项目配置合并后的原始 dict(向导同时改模型与渠道)。"""
    path = ensure_user_system_json()
    user = json.loads(path.read_text(encoding="utf-8"))
    return {**user, **read_project_config(project_config_path())}


def _save_raw(data: dict) -> Path:
    """校验后拆开写回:渠道(机器人)写当前项目的 config.json;其余只把非项目键写回用户级 system.json,
    用户文件里原有的 tools 等项目可覆盖的键保持原样(data 是合并视图,不能拿项目值覆盖用户值)。"""
    SystemConfig(**data)  # validate before write; raises on shape errors
    project_part = {k: data[k] for k in ("channels",) if k in data}
    path = user_config_dir() / "system.json"
    user_part = json.loads(path.read_text(encoding="utf-8") or "{}") if path.exists() else {}
    user_part = {k: v for k, v in user_part.items() if k not in PROJECT_ONLY_KEYS}
    user_part.update({k: v for k, v in data.items() if k not in PROJECT_KEYS})
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(user_part, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if project_part:
        update_project_config(project_config_path(), **project_part)
    return path


def _default_feishu_register() -> Optional[dict]:
    from xana.agent_adapter.channels.feishu_onboard import register_feishu
    return register_feishu()


def run_wizard(
    *,
    ask: AskFn,
    out: TextIO,
    feishu_register: FeishuRegisterFn = _default_feishu_register,
) -> int:
    data = _load_raw()
    changed = False

    existing_models = data.get("models") or []
    if not existing_models:
        # First-time: fields are required
        rc, did = _do_models(data, ask=ask, out=out)
    else:
        # Already configured: enter directly; Enter at first prompt skips
        names = ", ".join(m.get("name", "") for m in existing_models)
        print(f"[xana setup] 已配置模型: {names}", file=out)
        rc, did = _do_models_reconfigure(data, ask=ask, out=out)
    if rc != 0:
        return rc
    changed = changed or did

    rc, did = _do_channels(data, ask=ask, out=out, feishu_register=feishu_register)
    if rc != 0:
        return rc
    changed = changed or did

    if changed:
        path = _save_raw(data)
        print(f"[xana setup] 配置已写入 {path}", file=out)
    else:
        print("[xana setup] 模型与渠道均已配置,无需更改。", file=out)

    # daemon: ensure token exists in the project config, then surface endpoint hint.
    project_json = project_config_path()
    ensure_daemon_token(project_json)
    try:
        d = load_system_config().daemon
    except Exception:
        d = None
    if d is not None:
        print("", file=out)
        print(f"[xana setup] 项目 {project_id()} 的 daemon HTTP+SSE 已配置:", file=out)
        print(f"    unix socket {daemon_socket(d)}", file=out)
        print(
            f"    TCP 端口    {d.port}"
            f"(默认仅 unix;项目配置里把 daemon.bind 改成 '0.0.0.0' 暴露到 LAN — 慎重)",
            file=out,
        )
        print(f"    token       已写入 {project_json}(chmod 0600)", file=out)
    return 0


def _do_models(data: dict, *, ask: AskFn, out: TextIO) -> tuple[int, bool]:
    print("[xana setup] 配置模型(写入 system.json models[]):", file=out)
    print("  (Kimi 订阅用户请直接运行 `xana login` 走 OAuth,无需在此填 api key)", file=out)
    mtype = ask("  provider type (如 anthropic_deepseek / kimi): ").strip()
    name = ask("  模型名称 (自定, 如 ds-main): ").strip()
    api_key = ask("  api key: ").strip()
    base_url = ask("  base url (可选, 回车跳过): ").strip()
    if not mtype or not name or not api_key:
        print("error: type / 名称 / api key 均不能为空。", file=out)
        return 1, False
    entry: dict = {"name": name, "type": mtype, "api_key": api_key}
    if base_url:
        entry["base_url"] = base_url
    data.setdefault("models", []).append(entry)
    dm = dict(data.get("default_model") or {})
    if dm.get("chat") is None:
        dm["chat"] = name
    if dm.get("agent") is None:
        dm["agent"] = name
    data["default_model"] = dm
    print(f"[xana setup] v 模型 {name} ({mtype}) 已登记,默认 chat/agent。", file=out)
    return 0, True


def _do_models_reconfigure(data: dict, *, ask: AskFn, out: TextIO) -> tuple[int, bool]:
    """Reconfigure path: gather a new model entry, replace if same name, else append.
    Enter at the first prompt skips the step (existing config untouched).
    Then ask whether to set it as default chat/agent."""
    print("[xana setup] 配置模型(写入 system.json models[]):", file=out)
    mtype = ask("  provider type (如 anthropic_deepseek, 回车跳过): ").strip()
    if not mtype:
        print("[xana setup] 跳过模型配置(保留现有)。", file=out)
        return 0, False
    name = ask("  模型名称 (自定, 如 ds-main): ").strip()
    api_key = ask("  api key: ").strip()
    base_url = ask("  base url (可选, 回车跳过): ").strip()
    if not mtype or not name or not api_key:
        print("error: type / 名称 / api key 均不能为空。", file=out)
        return 1, False
    entry: dict = {"name": name, "type": mtype, "api_key": api_key}
    if base_url:
        entry["base_url"] = base_url
    models = list(data.get("models") or [])
    replaced = False
    for i, m in enumerate(models):
        if m.get("name") == name:
            models[i] = entry
            replaced = True
            break
    if not replaced:
        models.append(entry)
    data["models"] = models
    set_default_ans = ask("  设为默认 chat/agent? [Y/n]: ").strip().lower()
    if set_default_ans in ("", "y", "yes"):
        dm = dict(data.get("default_model") or {})
        dm["chat"] = name
        dm["agent"] = name
        data["default_model"] = dm
    action = "替换" if replaced else "追加"
    print(f"[xana setup] v 模型 {name} ({mtype}) 已{action}。", file=out)
    return 0, True


def _do_channels(
    data: dict, *, ask: AskFn, out: TextIO, feishu_register: FeishuRegisterFn
) -> tuple[int, bool]:
    print("[xana setup] 配置聊天渠道:", file=out)
    print("  1) 飞书 (扫码自动创建应用)", file=out)
    print("  2) Telegram (bot token)", file=out)
    print("  3) 跳过", file=out)
    choice = ask("  选择 [1/2/3, 回车跳过]: ").strip()
    if choice == "1":
        return _do_feishu(data, out=out, feishu_register=feishu_register)
    if choice == "2":
        return _do_telegram(data, ask=ask, out=out)
    print("[xana setup] 跳过渠道配置。", file=out)
    return 0, False


def _do_feishu(
    data: dict, *, out: TextIO, feishu_register: FeishuRegisterFn
) -> tuple[int, bool]:
    creds = feishu_register()
    if not creds:
        print("error: 飞书扫码注册失败或超时。", file=out)
        return 1, False
    if not creds.get("app_id") or not creds.get("app_secret"):
        print("error: 飞书注册返回的凭证不完整。", file=out)
        return 1, False
    channels = dict(data.get("channels") or {})
    fs = dict(channels.get("feishu") or {})
    fs.update({
        "app_id": creds["app_id"],
        "app_secret_key": creds["app_secret"],
        "domain": creds.get("domain", "feishu"),
        "open_id": creds.get("open_id"),
        "bot_name": creds.get("bot_name"),
        "bot_open_id": creds.get("bot_open_id"),
    })
    channels["feishu"] = fs
    data["channels"] = channels
    print(f"[xana setup] v 飞书应用 {creds['app_id']} 已配置 (bot: {creds.get('bot_name')})。", file=out)
    print("  所有用户(含你自己)首次发消息都需 `xana pair approve feishu <open_id>` 审批。", file=out)
    return 0, True


def _do_telegram(data: dict, *, ask: AskFn, out: TextIO) -> tuple[int, bool]:
    token = ask("  Telegram bot token (来自 @BotFather): ").strip()
    if not token or ":" not in token:
        print("error: bot token 形如 '123456:ABC-...'。", file=out)
        return 1, False
    allowed_raw = ask("  允许的 Telegram user_id (csv, 可选; 留空走配对审批): ").strip()
    try:
        allowed = [int(x) for x in allowed_raw.split(",") if x.strip()] if allowed_raw else []
    except ValueError:
        print("error: allowed_user_ids 必须是逗号分隔的整数。", file=out)
        return 1, False
    channels = dict(data.get("channels") or {})
    tg = dict(channels.get("telegram") or {})
    tg["bot_token"] = token
    if allowed:
        tg["allowed_user_ids"] = allowed
    channels["telegram"] = tg
    data["channels"] = channels
    print("[xana setup] v Telegram token 已保存。后续:", file=out)
    print("  1. 运行 `xana serve` 上线机器人;", file=out)
    print("  2. 用你的账号给机器人发一条消息,会收到配对提示;", file=out)
    print("  3. 运行 `xana pair approve telegram <user_id>` 批准。", file=out)
    return 0, True


def ensure_daemon_token(project_json: Path) -> None:
    """项目配置的 daemon.token 为空时生成 32 字节(64 位十六进制)写回;已有则保留。"""
    daemon = dict(read_project_config(project_json).get("daemon") or {})
    if not daemon.get("token"):
        daemon["token"] = secrets.token_hex(32)
        update_project_config(project_json, daemon=daemon)
