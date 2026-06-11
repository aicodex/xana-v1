# xana setup 向导 + 渠道配对 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `xana setup` 加交互式向导(模型配置 + 渠道配置:飞书扫码注册 / Telegram bot token),并实现配对(pairing)机制 —— 未配对用户首次发消息时机器人回复 pair 命令提示,管理员 `xana pair approve` 后才能开始对话。

**Architecture:** 全部建立在现有接口之上,不改任何 interfaces.py / contracts.py。配置写入已有的 `~/.xana/config/system.json`(pydantic `SystemConfig` 已含 `channels.telegram` / `channels.feishu`,无需改 schema)。新增 `PairingStore`(仿 `BindingStore` 的 JSON 原子写存储,带 mtime 热重载,存于 `~/.xana/channels/pairings.json`)。飞书扫码移植 yuxu_backup 的 device-code 流程(`feishu_onboard.py`:init → begin → 轮询 → probe_bot)。适配器层在 `TelegramAdapter._dispatch_update` / `FeishuAdapter._on_event` 入口做配对门禁(可选注入 `PairingStore`,None 时行为不变,保证既有测试零改动)。`xana serve` 改为优先读 system.json 的 channels(环境变量覆盖),并接入 PairingStore。

**Tech Stack:** Python 3.11+、httpx(已有依赖)、pydantic(已有)、qrcode(可选依赖,缺失时退化为打印 URL)、pytest + pytest-asyncio。

**参考实现(只读,不可复制 yuxu/,只看 yuxu_backup/):**
- `/home/xzp/project/yuxu_backup/src/yuxu/services/gateway/adapters/feishu_onboard.py` — 飞书 device-code 扫码流程
- `/home/xzp/project/yuxu_backup/src/yuxu/services/gateway/pairing.py` — PairingRegistry
- `/home/xzp/project/yuxu_backup/src/yuxu/cli/setup_wizard.py` — 向导流程

**关键现有代码(实现者必读):**
- `src/xana/config/loader.py` — `user_config_dir()`, `ensure_user_system_json()`, `JsonSystemConfigReader`
- `src/xana/config/system_model.py` — `SystemConfig`/`ModelEntry`/`Channels`(已支持 telegram/feishu,**不要改 schema**;`TelegramChannel`/`FeishuChannel` 是 `extra="allow"`,可存额外字段如 open_id)
- `src/xana/agent_adapter/channels/_bindings.py` — `BindingStore` 原子写模式(`tmp = path.parent / (path.name + ".tmp")` 然后 `os.replace`)
- `src/xana/agent_adapter/channels/telegram.py` / `feishu.py` — 适配器(构造函数可以加参数;`Adapter` 接口本身不可改)
- `src/xana/cli/serve.py` — 现有 env-only 装配
- `src/xana/cli/xana.py` — argparse 子命令注册模式

**约束(全局,所有任务适用):**
- 绝不修改任何 `interfaces.py` / `contracts.py`。
- 只做本地 commit,绝不 push。
- 已知既有失败测试:`test_xanac_empty_input_exits_nonzero` —— 与本计划无关,忽略。

---

### Task 1: PairingStore — 配对状态存储

**Files:**
- Create: `src/xana/agent_adapter/channels/_pairing.py`
- Test: `tests/test_pairing_store.py`

存储格式(`~/.xana/channels/pairings.json`,chmod 600):

```json
{
  "telegram": {
    "allowed": {"12345": {"approved_at": "2026-06-12T10:00:00+00:00", "note": "Bob"}},
    "pending": {"67890": {"first_seen": "2026-06-12T11:00:00+00:00", "first_message": "hello", "chat_id": "67890"}}
  },
  "feishu": {"allowed": {}, "pending": {}}
}
```

- [x] **Step 1: 写失败测试**

```python
# tests/test_pairing_store.py
"""PairingStore: per-platform allowlist + pending queue with hot reload."""
from pathlib import Path

from xana.agent_adapter.channels._pairing import PairingStore


def test_unknown_user_not_allowed(tmp_path: Path) -> None:
    store = PairingStore(path=tmp_path / "pairings.json")
    assert store.is_allowed("telegram", "12345") is False


def test_add_pending_then_approve(tmp_path: Path) -> None:
    store = PairingStore(path=tmp_path / "pairings.json")
    added = store.add_pending("telegram", "12345", first_message="hi", chat_id="12345")
    assert added is True
    # 重复 add_pending 不覆盖、返回 False
    assert store.add_pending("telegram", "12345", first_message="again") is False
    assert ("telegram", "12345") in [(p, e["user_id"]) for p, e in store.list_pending()]

    assert store.approve("telegram", "12345", note="Bob") is True
    assert store.is_allowed("telegram", "12345") is True
    assert store.list_pending() == []
    # approve 不存在的 pending 返回 False
    assert store.approve("telegram", "nope") is False


def test_reject_and_revoke(tmp_path: Path) -> None:
    store = PairingStore(path=tmp_path / "pairings.json")
    store.add_pending("feishu", "ou_abc", first_message="hi")
    assert store.reject("feishu", "ou_abc") is True
    assert store.list_pending() == []
    assert store.reject("feishu", "ou_abc") is False

    store.allow("feishu", "ou_admin", note="admin self-pair")
    assert store.is_allowed("feishu", "ou_admin") is True
    assert store.revoke("feishu", "ou_admin") is True
    assert store.is_allowed("feishu", "ou_admin") is False


def test_persistence_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "pairings.json"
    PairingStore(path=path).allow("telegram", "1", note="x")
    again = PairingStore(path=path)
    assert again.is_allowed("telegram", "1") is True


def test_hot_reload_detects_external_write(tmp_path: Path) -> None:
    """serve 进程持有的 store 能看到 CLI 进程 approve 后的变化。"""
    path = tmp_path / "pairings.json"
    serve_side = PairingStore(path=path)
    assert serve_side.is_allowed("telegram", "9") is False

    cli_side = PairingStore(path=path)
    cli_side.allow("telegram", "9")
    # is_allowed 内部按 mtime 热重载
    assert serve_side.is_allowed("telegram", "9") is True


def test_list_filters_by_platform(tmp_path: Path) -> None:
    store = PairingStore(path=tmp_path / "pairings.json")
    store.allow("telegram", "1")
    store.allow("feishu", "ou_a")
    assert len(store.list_allowed()) == 2
    assert len(store.list_allowed(platform="feishu")) == 1
    assert store.list_allowed(platform="feishu")[0][1]["user_id"] == "ou_a"
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_pairing_store.py -v`
Expected: FAIL(ModuleNotFoundError: `_pairing`)

- [x] **Step 3: 最小实现**

```python
# src/xana/agent_adapter/channels/_pairing.py
"""PairingStore: per-platform user allowlist + pending queue.

JSON sidecar at ~/.xana/channels/pairings.json, atomic writes, mtime-based
hot reload so a long-running `xana serve` sees approvals made by the
`xana pair` CLI in another process. Mirrors yuxu_backup's PairingRegistry
semantics but stores JSON (xana convention) instead of YAML.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PairingStore:
    def __init__(self, *, path: Path) -> None:
        self._path = path
        self._mtime: float | None = None
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._reload_if_changed()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_allowed(self, platform: str, user_id: str) -> bool:
        self._reload_if_changed()
        return user_id in self._platform(platform)["allowed"]

    def list_allowed(self, platform: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        return self._list("allowed", platform)

    def list_pending(self, platform: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        return self._list("pending", platform)

    # ------------------------------------------------------------------
    # Mutations (each reloads first so concurrent writers don't clobber)
    # ------------------------------------------------------------------

    def add_pending(
        self, platform: str, user_id: str, *, first_message: str = "", chat_id: str | None = None
    ) -> bool:
        self._reload_if_changed()
        bucket = self._platform(platform)
        if user_id in bucket["allowed"] or user_id in bucket["pending"]:
            return False
        bucket["pending"][user_id] = {
            "first_seen": _now(),
            "first_message": first_message[:200],
            "chat_id": chat_id,
        }
        self._save()
        return True

    def approve(self, platform: str, user_id: str, *, note: str = "") -> bool:
        self._reload_if_changed()
        bucket = self._platform(platform)
        if user_id not in bucket["pending"]:
            return False
        del bucket["pending"][user_id]
        bucket["allowed"][user_id] = {"approved_at": _now(), "note": note}
        self._save()
        return True

    def reject(self, platform: str, user_id: str) -> bool:
        self._reload_if_changed()
        bucket = self._platform(platform)
        if user_id not in bucket["pending"]:
            return False
        del bucket["pending"][user_id]
        self._save()
        return True

    def revoke(self, platform: str, user_id: str) -> bool:
        self._reload_if_changed()
        bucket = self._platform(platform)
        if user_id not in bucket["allowed"]:
            return False
        del bucket["allowed"][user_id]
        self._save()
        return True

    def allow(self, platform: str, user_id: str, *, note: str = "") -> None:
        """Directly allowlist (admin self-pair during setup)."""
        self._reload_if_changed()
        bucket = self._platform(platform)
        bucket["pending"].pop(user_id, None)
        bucket["allowed"][user_id] = {"approved_at": _now(), "note": note}
        self._save()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _platform(self, platform: str) -> dict[str, dict[str, Any]]:
        return self._data.setdefault(platform, {"allowed": {}, "pending": {}})

    def _list(self, kind: str, platform: str | None) -> list[tuple[str, dict[str, Any]]]:
        self._reload_if_changed()
        out: list[tuple[str, dict[str, Any]]] = []
        for plat, bucket in sorted(self._data.items()):
            if platform is not None and plat != platform:
                continue
            for user_id, entry in bucket.get(kind, {}).items():
                out.append((plat, {"user_id": user_id, **entry}))
        return out

    def _reload_if_changed(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self._mtime:
            return
        self._data = json.loads(self._path.read_text(encoding="utf-8"))
        self._mtime = mtime

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.parent / (self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
        try:
            self._path.chmod(0o600)
        except OSError:
            pass
        self._mtime = self._path.stat().st_mtime
```

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_pairing_store.py -v`
Expected: 6 PASS

- [x] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/channels/_pairing.py tests/test_pairing_store.py
git commit -m "feat(channels): PairingStore — per-platform allowlist with pending queue and hot reload"
```

---

### Task 2: `xana pair` CLI 子命令

**Files:**
- Create: `src/xana/cli/commands/pair.py`
- Modify: `src/xana/cli/xana.py`(注册子命令)
- Test: `tests/test_pair_cli.py`

子命令:`xana pair list [--platform P]` / `xana pair approve P USER_ID [--note "..."]` / `xana pair reject P USER_ID` / `xana pair revoke P USER_ID`。

- [x] **Step 1: 写失败测试**

```python
# tests/test_pair_cli.py
"""xana pair: list/approve/reject/revoke against PairingStore."""
import io
from pathlib import Path

from xana.agent_adapter.channels._pairing import PairingStore
from xana.cli.xana import main


def _run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, out=out)
    return code, out.getvalue()


def test_pair_approve_moves_pending_to_allowed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XANA_HOME", str(tmp_path))
    store = PairingStore(path=tmp_path / "channels" / "pairings.json")
    store.add_pending("telegram", "67890", first_message="hello")

    code, output = _run(["pair", "approve", "telegram", "67890", "--note", "Bob"])
    assert code == 0
    assert "approved telegram:67890" in output
    assert PairingStore(path=tmp_path / "channels" / "pairings.json").is_allowed("telegram", "67890")


def test_pair_approve_unknown_user_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XANA_HOME", str(tmp_path))
    code, output = _run(["pair", "approve", "telegram", "nope"])
    assert code == 1
    assert "no pending" in output


def test_pair_list_shows_allowed_and_pending(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XANA_HOME", str(tmp_path))
    store = PairingStore(path=tmp_path / "channels" / "pairings.json")
    store.allow("feishu", "ou_admin", note="admin")
    store.add_pending("telegram", "111", first_message="hi there")

    code, output = _run(["pair", "list"])
    assert code == 0
    assert "ou_admin" in output
    assert "111" in output
    assert "hi there" in output


def test_pair_reject_and_revoke(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XANA_HOME", str(tmp_path))
    path = tmp_path / "channels" / "pairings.json"
    store = PairingStore(path=path)
    store.add_pending("telegram", "1")
    store.allow("telegram", "2")

    assert _run(["pair", "reject", "telegram", "1"])[0] == 0
    assert _run(["pair", "revoke", "telegram", "2"])[0] == 0
    fresh = PairingStore(path=path)
    assert fresh.list_pending() == []
    assert fresh.is_allowed("telegram", "2") is False
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_pair_cli.py -v`
Expected: FAIL(argparse: invalid choice 'pair')

- [x] **Step 3: 实现 pair.py**

```python
# src/xana/cli/commands/pair.py
"""xana pair — manage channel user pairing (allowlist).

Pairing file lives next to channel bindings: $XANA_HOME/channels/pairings.json
(default ~/.xana/channels/pairings.json), shared with the running serve
daemon via PairingStore's mtime hot reload — no restart needed after approve.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TextIO

from xana.agent_adapter.channels._pairing import PairingStore


def pairings_path() -> Path:
    home = os.environ.get("XANA_HOME")
    root = Path(home).expanduser() if home else Path.home() / ".xana"
    return root / "channels" / "pairings.json"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="show allowed + pending users").add_argument(
        "--platform", default=None
    )

    for action in ("approve", "reject", "revoke"):
        p = sub.add_parser(action, help=f"{action} a user")
        p.add_argument("platform", choices=("telegram", "feishu"))
        p.add_argument("user_id")
        if action == "approve":
            p.add_argument("--note", default="")


def run(args: argparse.Namespace, *, out: TextIO) -> int:
    store = PairingStore(path=pairings_path())

    if args.action == "list":
        allowed = store.list_allowed(platform=args.platform)
        pending = store.list_pending(platform=args.platform)
        print(f"[xana pair] file: {pairings_path()}", file=out)
        print(f"[xana pair] allowed ({len(allowed)}):", file=out)
        for plat, e in allowed:
            print(f"  v {plat:<10} {e['user_id']:<28} {e.get('approved_at', '')} {e.get('note', '')}", file=out)
        print(f"[xana pair] pending ({len(pending)}):", file=out)
        for plat, e in pending:
            print(f"  ? {plat:<10} {e['user_id']:<28} {e.get('first_seen', '')} {e.get('first_message', '')!r}", file=out)
        return 0

    if args.action == "approve":
        if store.approve(args.platform, args.user_id, note=args.note):
            print(f"[xana pair] approved {args.platform}:{args.user_id}", file=out)
            return 0
        print(f"[xana pair] no pending entry {args.platform}:{args.user_id}", file=out)
        return 1

    if args.action == "reject":
        if store.reject(args.platform, args.user_id):
            print(f"[xana pair] rejected {args.platform}:{args.user_id}", file=out)
            return 0
        print(f"[xana pair] no pending entry {args.platform}:{args.user_id}", file=out)
        return 1

    # revoke
    if store.revoke(args.platform, args.user_id):
        print(f"[xana pair] revoked {args.platform}:{args.user_id}", file=out)
        return 0
    print(f"[xana pair] not in allowed list: {args.platform}:{args.user_id}", file=out)
    return 1
```

在 `src/xana/cli/xana.py` 的 `_build_parser()` 中(serve 注册之后)加:

```python
    from xana.cli.commands import pair as pair_cmd
    pair_parser = subparsers.add_parser(
        "pair", help="manage channel user pairing (list/approve/reject/revoke)"
    )
    pair_cmd.add_arguments(pair_parser)
    pair_parser.set_defaults(func=pair_cmd.run)
```

(import 放文件顶部与其他 commands import 并列即可,无循环依赖。)

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_pair_cli.py tests/test_pairing_store.py -v`
Expected: 全部 PASS

- [x] **Step 5: Commit**

```bash
git add src/xana/cli/commands/pair.py src/xana/cli/xana.py tests/test_pair_cli.py
git commit -m "feat(cli): xana pair — list/approve/reject/revoke channel pairings"
```

---

### Task 3: 飞书扫码注册(device-code 流程)

**Files:**
- Create: `src/xana/agent_adapter/channels/feishu_onboard.py`
- Test: `tests/test_feishu_onboard.py`
- Modify: `pyproject.toml`(qrcode 加入可选 extra)

移植 yuxu_backup `feishu_onboard.py` 的流程:`init` → `begin`(拿 device_code + 可扫码 URL)→ 终端渲染二维码 → 轮询直到拿到 `client_id/client_secret` → `probe_bot` 验证并取 bot 信息。同步代码(CLI 场景),HTTP 通过可注入的 `post_json` / `get_json` 便于测试。

端点:
- 注册:`https://accounts.feishu.cn/oauth/v1/app/registration`(lark 域:`accounts.larksuite.com`)
- open API:`https://open.feishu.cn`(lark:`open.larksuite.com`)

- [x] **Step 1: 写失败测试**

```python
# tests/test_feishu_onboard.py
"""feishu_onboard: device-code registration flow with injectable HTTP."""
from xana.agent_adapter.channels.feishu_onboard import (
    begin_registration,
    poll_registration,
    probe_bot,
    register_feishu,
)


def test_begin_registration_returns_device_fields() -> None:
    calls = []

    def fake_post(url: str, body: dict) -> dict:
        calls.append((url, body))
        if body["action"] == "init":
            return {"supported_auth_methods": ["client_secret"]}
        return {
            "device_code": "dc-1",
            "verification_uri_complete": "https://accounts.feishu.cn/qr/abc",
            "user_code": "ABCD",
            "interval": 0,
            "expire_in": 60,
        }

    begin = begin_registration(domain="feishu", post_json=fake_post)
    assert begin["device_code"] == "dc-1"
    assert begin["qr_url"] == "https://accounts.feishu.cn/qr/abc"
    assert calls[0][1]["action"] == "init"
    assert calls[1][1]["action"] == "begin"
    assert "accounts.feishu.cn" in calls[0][0]


def test_poll_returns_credentials_on_success() -> None:
    responses = iter([
        {"error": "authorization_pending"},
        {"client_id": "cli_x", "client_secret": "sec_y", "user_info": {"open_id": "ou_admin"}},
    ])

    result = poll_registration(
        device_code="dc-1", interval=0, expire_in=60, domain="feishu",
        post_json=lambda url, body: next(responses),
    )
    assert result == {
        "app_id": "cli_x", "app_secret": "sec_y",
        "domain": "feishu", "open_id": "ou_admin",
    }


def test_poll_returns_none_on_denied() -> None:
    result = poll_registration(
        device_code="dc-1", interval=0, expire_in=60, domain="feishu",
        post_json=lambda url, body: {"error": "access_denied"},
    )
    assert result is None


def test_probe_bot_fetches_name() -> None:
    def fake_post(url: str, body: dict) -> dict:
        assert "tenant_access_token" in url
        return {"code": 0, "tenant_access_token": "t-xyz"}

    def fake_get(url: str, headers: dict) -> dict:
        assert headers["Authorization"] == "Bearer t-xyz"
        return {"code": 0, "bot": {"bot_name": "xana-bot", "open_id": "ou_bot"}}

    info = probe_bot("cli_x", "sec_y", "feishu", post_json=fake_post, get_json=fake_get)
    assert info == {"bot_name": "xana-bot", "bot_open_id": "ou_bot"}


def test_register_feishu_end_to_end(capsys) -> None:
    state = {"polls": 0}

    def fake_post(url: str, body: dict) -> dict:
        if "registration" in url:
            action = body.get("action")
            if action == "init":
                return {"supported_auth_methods": ["client_secret"]}
            if action == "begin":
                return {"device_code": "dc", "verification_uri_complete": "https://q/r",
                        "interval": 0, "expire_in": 60}
            state["polls"] += 1
            return {"client_id": "cli", "client_secret": "sec", "user_info": {"open_id": "ou_a"}}
        return {"code": 0, "tenant_access_token": "tok"}

    def fake_get(url: str, headers: dict) -> dict:
        return {"code": 0, "bot": {"bot_name": "b", "open_id": "ou_b"}}

    result = register_feishu(post_json=fake_post, get_json=fake_get)
    assert result == {
        "app_id": "cli", "app_secret": "sec", "domain": "feishu",
        "open_id": "ou_a", "bot_name": "b", "bot_open_id": "ou_b",
    }
    assert "https://q/r" in capsys.readouterr().out
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_feishu_onboard.py -v`
Expected: FAIL(ModuleNotFoundError)

- [x] **Step 3: 实现 feishu_onboard.py**

```python
# src/xana/agent_adapter/channels/feishu_onboard.py
"""Feishu scan-to-create app registration (device-code flow).

Ported from yuxu_backup feishu_onboard.py. Synchronous on purpose — this
runs inside the `xana setup` CLI wizard, not the async serve runtime.
HTTP is injectable (post_json/get_json) so tests never touch the network.

Flow: init (capability check) -> begin (device_code + QR url) -> render QR
in terminal -> poll until the user scans & confirms -> probe_bot for the
bot's display name / open_id.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

PostJson = Callable[[str, dict], dict]
GetJson = Callable[[str, dict], dict]

_ACCOUNTS_BASE = {"feishu": "https://accounts.feishu.cn", "lark": "https://accounts.larksuite.com"}
_OPEN_BASE = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}


def _default_post(url: str, body: dict) -> dict:
    import httpx
    resp = httpx.post(url, json=body, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _default_get(url: str, headers: dict) -> dict:
    import httpx
    resp = httpx.get(url, headers=headers, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def begin_registration(*, domain: str = "feishu", post_json: PostJson = _default_post) -> dict:
    """init + begin; returns {device_code, qr_url, user_code, interval, expire_in}."""
    url = f"{_ACCOUNTS_BASE[domain]}/oauth/v1/app/registration"
    init = post_json(url, {"action": "init"})
    methods = init.get("supported_auth_methods") or []
    if "client_secret" not in methods:
        raise RuntimeError(f"feishu registration: client_secret unsupported (got {methods})")
    begin = post_json(url, {
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id",
    })
    return {
        "device_code": begin["device_code"],
        "qr_url": begin["verification_uri_complete"],
        "user_code": begin.get("user_code"),
        "interval": int(begin.get("interval", 5)),
        "expire_in": int(begin.get("expire_in", 600)),
    }


def poll_registration(
    *,
    device_code: str,
    interval: int,
    expire_in: int,
    domain: str = "feishu",
    post_json: PostJson = _default_post,
) -> Optional[dict]:
    """Poll until scanned. Returns {app_id, app_secret, domain, open_id} or None."""
    url = f"{_ACCOUNTS_BASE[domain]}/oauth/v1/app/registration"
    deadline = time.monotonic() + expire_in
    while time.monotonic() <= deadline:
        res = post_json(url, {"action": "poll", "device_code": device_code, "tp": "ob_app"})
        user_info = res.get("user_info") or {}
        if res.get("client_id") and res.get("client_secret"):
            return {
                "app_id": res["client_id"],
                "app_secret": res["client_secret"],
                "domain": "lark" if user_info.get("tenant_brand") == "lark" else domain,
                "open_id": user_info.get("open_id"),
            }
        if res.get("error") in ("access_denied", "expired_token"):
            return None
        time.sleep(interval)
    return None


def probe_bot(
    app_id: str,
    app_secret: str,
    domain: str,
    *,
    post_json: PostJson = _default_post,
    get_json: GetJson = _default_get,
) -> dict:
    """Fetch bot display info; raises RuntimeError on logical error codes."""
    base = _OPEN_BASE[domain]
    tok = post_json(
        f"{base}/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    if tok.get("code", 0) != 0:
        raise RuntimeError(f"feishu tenant_access_token error: {tok}")
    info = get_json(
        f"{base}/open-apis/bot/v3/info",
        {"Authorization": f"Bearer {tok['tenant_access_token']}"},
    )
    if info.get("code", 0) != 0:
        raise RuntimeError(f"feishu bot info error: {info}")
    bot = info.get("bot") or {}
    return {"bot_name": bot.get("bot_name"), "bot_open_id": bot.get("open_id")}


def render_qr(url: str) -> bool:
    """Print a terminal QR for the url. Returns False if qrcode lib missing."""
    try:
        import qrcode  # type: ignore[import-not-found]
    except ImportError:
        return False
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    return True


def register_feishu(
    *,
    initial_domain: str = "feishu",
    timeout_seconds: int = 600,
    post_json: PostJson = _default_post,
    get_json: GetJson = _default_get,
) -> Optional[dict]:
    """Full flow. Returns {app_id, app_secret, domain, open_id, bot_name, bot_open_id} or None."""
    begin = begin_registration(domain=initial_domain, post_json=post_json)
    if render_qr(begin["qr_url"]):
        print(f"扫描上方二维码,或打开:\n{begin['qr_url']}")
    else:
        print(f"打开此链接完成授权:\n{begin['qr_url']}\n提示:`pip install qrcode` 可在终端直接显示二维码。")
    result = poll_registration(
        device_code=begin["device_code"],
        interval=begin["interval"],
        expire_in=min(begin["expire_in"], timeout_seconds),
        domain=initial_domain,
        post_json=post_json,
    )
    if result is None:
        return None
    result.update(probe_bot(result["app_id"], result["app_secret"], result["domain"],
                            post_json=post_json, get_json=get_json))
    return result
```

`pyproject.toml` 在 `[project.optional-dependencies]`(没有则新建该表)加:

```toml
[project.optional-dependencies]
qr = ["qrcode>=7.4"]
```

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_feishu_onboard.py -v`
Expected: 5 PASS

- [x] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/channels/feishu_onboard.py tests/test_feishu_onboard.py pyproject.toml
git commit -m "feat(channels): feishu_onboard — scan-to-create device-code registration flow"
```

---

### Task 4: setup 交互向导(模型 + 渠道)

**Files:**
- Create: `src/xana/cli/commands/setup_wizard.py`
- Modify: `src/xana/cli/commands/setup.py`(末尾接入向导 + `--no-wizard`)
- Test: `tests/test_setup_wizard.py`

向导通过注入的 `ask`(input 函数)和 `out` 驱动,纯同步;幂等(已配置的段落跳过)。模型步:输入 provider type / 名称 / api_key / base_url,设 `default_model.chat` 和 `.agent`。渠道步:选 1=飞书(扫码,写入 channels.feishu 并把管理员 open_id 自动 self-pair)/ 2=Telegram(token + 可选 allowed ids)/ 3=跳过。配置读写直接操作 system.json 原始 dict(写前用 `SystemConfig(**data)` 校验),原子写 + chmod 600。

- [x] **Step 1: 写失败测试**

```python
# tests/test_setup_wizard.py
"""setup wizard: interactive model + channel configuration."""
import io
import json
from pathlib import Path

from xana.agent_adapter.channels._pairing import PairingStore
from xana.cli.commands.setup_wizard import run_wizard


def _make_ask(answers: list[str]):
    it = iter(answers)
    return lambda prompt: next(it)


def _read_config(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "config" / "system.json").read_text(encoding="utf-8"))


def test_wizard_configures_model_and_telegram(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XANA_HOME", str(tmp_path))
    out = io.StringIO()
    ask = _make_ask([
        "anthropic_deepseek",        # model type
        "ds-main",                   # model name
        "sk-test-123",               # api key
        "",                          # base_url (默认)
        "2",                         # channel choice: telegram
        "123456:ABC-token",          # bot token
        "",                          # allowed user ids (空 = 走 pairing)
    ])
    code = run_wizard(ask=ask, out=out)
    assert code == 0

    data = _read_config(tmp_path)
    entry = data["models"][0]
    assert entry["name"] == "ds-main"
    assert entry["type"] == "anthropic_deepseek"
    assert entry["api_key"] == "sk-test-123"
    assert data["default_model"]["chat"] == "ds-main"
    assert data["default_model"]["agent"] == "ds-main"
    assert data["channels"]["telegram"]["bot_token"] == "123456:ABC-token"


def test_wizard_telegram_rejects_bad_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XANA_HOME", str(tmp_path))
    out = io.StringIO()
    ask = _make_ask([
        "anthropic_deepseek", "m", "k", "",
        "2",
        "not-a-token",               # 无冒号 → 报错,该步失败但向导整体返回 1
    ])
    code = run_wizard(ask=ask, out=out)
    assert code == 1
    assert "bot token" in out.getvalue()


def test_wizard_feishu_saves_creds_and_self_pairs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XANA_HOME", str(tmp_path))
    out = io.StringIO()
    fake_creds = {
        "app_id": "cli_x", "app_secret": "sec_y", "domain": "feishu",
        "open_id": "ou_admin", "bot_name": "xb", "bot_open_id": "ou_bot",
    }
    ask = _make_ask(["anthropic_deepseek", "m", "k", "", "1"])
    code = run_wizard(ask=ask, out=out, feishu_register=lambda: fake_creds)
    assert code == 0

    data = _read_config(tmp_path)
    fs = data["channels"]["feishu"]
    assert fs["app_id"] == "cli_x"
    assert fs["app_secret_key"] == "sec_y"
    assert fs["open_id"] == "ou_admin"
    assert fs["bot_open_id"] == "ou_bot"
    # 管理员已自动配对
    store = PairingStore(path=tmp_path / "channels" / "pairings.json")
    assert store.is_allowed("feishu", "ou_admin") is True


def test_wizard_skips_already_configured_sections(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XANA_HOME", str(tmp_path))
    out = io.StringIO()
    # 第一轮:配置模型 + telegram
    run_wizard(ask=_make_ask(["t", "m", "k", "", "2", "1:ok", ""]), out=out)
    # 第二轮:全部已配置 → 不需要任何回答即可完成
    code = run_wizard(ask=_make_ask([]), out=io.StringIO())
    assert code == 0


def test_wizard_channel_skip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XANA_HOME", str(tmp_path))
    code = run_wizard(ask=_make_ask(["t", "m", "k", "", "3"]), out=io.StringIO())
    assert code == 0
    data = _read_config(tmp_path)
    assert data["channels"]["telegram"]["bot_token"] == ""
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_setup_wizard.py -v`
Expected: FAIL(ModuleNotFoundError)

- [x] **Step 3: 实现 setup_wizard.py**

```python
# src/xana/cli/commands/setup_wizard.py
"""Interactive setup wizard: model entries + channel (Feishu QR / Telegram).

Mirrors yuxu_backup setup_wizard.py but minimal for xana v1:
  * model step  — one ModelEntry + default_model.chat/.agent
  * channel step — 1) Feishu scan-to-create  2) Telegram bot token  3) skip

Idempotent: sections that already have config are skipped, so re-running
`xana setup` only asks for what's missing. All answers flow through the
injected `ask` callable; tests drive it with canned answers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional, TextIO

from xana.agent_adapter.channels._pairing import PairingStore
from xana.cli.commands.pair import pairings_path
from xana.config.loader import ensure_user_system_json, user_config_dir
from xana.config.system_model import SystemConfig

AskFn = Callable[[str], str]
FeishuRegisterFn = Callable[[], Optional[dict]]


def _load_raw() -> dict:
    path = ensure_user_system_json()
    return json.loads(path.read_text(encoding="utf-8"))


def _save_raw(data: dict) -> Path:
    SystemConfig(**data)  # validate before write; raises on shape errors
    path = user_config_dir() / "system.json"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
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

    if not data.get("models"):
        rc, did = _do_models(data, ask=ask, out=out)
        if rc != 0:
            return rc
        changed = changed or did

    channels = data.get("channels") or {}
    has_channel = bool(
        (channels.get("telegram") or {}).get("bot_token")
        or (channels.get("feishu") or {}).get("app_id")
    )
    if not has_channel:
        rc, did = _do_channels(data, ask=ask, out=out, feishu_register=feishu_register)
        if rc != 0:
            return rc
        changed = changed or did

    if changed:
        path = _save_raw(data)
        print(f"[xana setup] 配置已写入 {path}", file=out)
    else:
        print("[xana setup] 模型与渠道均已配置,无需更改。", file=out)
    return 0


def _do_models(data: dict, *, ask: AskFn, out: TextIO) -> tuple[int, bool]:
    print("[xana setup] 配置模型(写入 system.json models[]):", file=out)
    mtype = ask("  provider type (如 anthropic_deepseek): ").strip()
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
    dm.setdefault("chat", name)
    dm.setdefault("agent", name)
    # setdefault 不会覆盖 None,显式处理
    if dm.get("chat") is None:
        dm["chat"] = name
    if dm.get("agent") is None:
        dm["agent"] = name
    data["default_model"] = dm
    print(f"[xana setup] v 模型 {name} ({mtype}) 已登记,默认 chat/agent。", file=out)
    return 0, True


def _do_channels(
    data: dict, *, ask: AskFn, out: TextIO, feishu_register: FeishuRegisterFn
) -> tuple[int, bool]:
    print("[xana setup] 配置聊天渠道:", file=out)
    print("  1) 飞书 (扫码自动创建应用)", file=out)
    print("  2) Telegram (bot token)", file=out)
    print("  3) 跳过", file=out)
    choice = ask("  选择 [1/2/3]: ").strip()
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
    if creds.get("open_id"):
        PairingStore(path=pairings_path()).allow(
            "feishu", creds["open_id"], note="admin self-pair (setup)"
        )
        print(f"[xana setup] v 管理员 {creds['open_id']} 已自动配对。", file=out)
    print(f"[xana setup] v 飞书应用 {creds['app_id']} 已配置 (bot: {creds.get('bot_name')})。", file=out)
    return 0, True


def _do_telegram(data: dict, *, ask: AskFn, out: TextIO) -> tuple[int, bool]:
    token = ask("  Telegram bot token (来自 @BotFather): ").strip()
    if not token or ":" not in token:
        print("error: bot token 形如 '123456:ABC-...'。", file=out)
        return 1, False
    allowed_raw = ask("  允许的 Telegram user_id (csv, 可选; 留空走配对审批): ").strip()
    allowed = [int(x) for x in allowed_raw.split(",") if x.strip()] if allowed_raw else []
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
```

在 `src/xana/cli/commands/setup.py` 中接入:

```python
# add_arguments 改为:
def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-wizard", action="store_true",
        help="skip the interactive model/channel wizard",
    )
```

`run()` 末尾(`return 0` 之前)加:

```python
    if not args.no_wizard and sys.stdin.isatty():
        from xana.cli.commands.setup_wizard import run_wizard
        print(file=out)
        return run_wizard(ask=input, out=out)
    return 0
```

(注意:既有 `tests/` 里若有直接调用 `setup_cmd.run(argparse.Namespace())` 的测试,需要补 `no_wizard=True` 字段;实现者跑全量测试确认。非 tty 下自动跳过向导,CI 安全。)

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_setup_wizard.py -v && python -m pytest tests/ -x -q -k "setup"`
Expected: 全部 PASS

- [x] **Step 5: Commit**

```bash
git add src/xana/cli/commands/setup_wizard.py src/xana/cli/commands/setup.py tests/test_setup_wizard.py
git commit -m "feat(cli): xana setup interactive wizard — model entry + feishu QR / telegram channel"
```

---

### Task 5: 适配器配对门禁(未配对 → 回 pair 提示并拦截)

**Files:**
- Modify: `src/xana/agent_adapter/channels/telegram.py`
- Modify: `src/xana/agent_adapter/channels/feishu.py`
- Test: `tests/test_adapter_pairing_gate.py`

两个适配器构造函数新增可选参数 `pairing: PairingStore | None = None`(默认 None = 不门禁,既有行为与测试不变)。门禁位置在入站入口(`_dispatch_update` / `_on_event`):

- `is_allowed` → 放行(原逻辑)。
- 否则 `add_pending(...)`;**仅当首次加入 pending(add_pending 返回 True)时**回复一条配对提示消息(避免每条消息都刷提示),然后丢弃该 inbound(不入队)。

提示文案:
- Telegram:`您尚未配对。请管理员在服务器上运行:\nxana pair approve telegram {user_id}`
- 飞书卡片:`您尚未配对。请管理员运行: xana pair approve feishu {open_id}`

注意:Telegram 的 `allowed_user_ids` 静态白名单逻辑保留——非空时仍然先按它过滤(两层防御);pairing 门禁在其后。FeishuAdapter 回复用 `self._api_sender.send_card`(`_on_event` 在 webhook 流程里只会在 `start()` 之后被调用,sender 已就绪;为 None 时只记 pending 不回复)。

- [x] **Step 1: 写失败测试**

```python
# tests/test_adapter_pairing_gate.py
"""Pairing gate: unpaired users get a pair-hint reply and no inbound enqueue."""
import asyncio
from pathlib import Path

import pytest

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels._pairing import PairingStore
from xana.agent_adapter.channels.feishu import FeishuAdapter, FeishuConfig
from xana.agent_adapter.channels.telegram import TelegramAdapter, TelegramConfig


class FakeTgClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, *, chat_id, text, parse_mode=None) -> int:
        self.sent.append((chat_id, text))
        return len(self.sent)

    async def edit_message_text(self, *, chat_id, message_id, text, parse_mode=None) -> None:
        pass

    async def get_updates(self, *, offset, timeout):
        return []


class FakeFeishuSender:
    def __init__(self) -> None:
        self.cards: list[tuple[str, dict]] = []

    async def send_card(self, *, receive_id, card) -> str:
        self.cards.append((receive_id, card))
        return f"om_{len(self.cards)}"

    async def patch_card(self, *, message_id, card) -> None:
        pass


def _tg_update(user_id: int, text: str) -> dict:
    return {"update_id": 1, "message": {
        "text": text, "from": {"id": user_id},
        "chat": {"id": user_id}, "message_id": 7,
    }}


def _feishu_event(open_id: str, chat_id: str, text: str) -> dict:
    import json
    return {
        "header": {"event_type": "im.message.receive_v1", "token": "tok"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "message_id": "om_x", "chat_id": chat_id,
                "message_type": "text", "content": json.dumps({"text": text}),
            },
        },
    }


@pytest.mark.asyncio
async def test_telegram_unpaired_gets_hint_and_no_inbound(tmp_path: Path) -> None:
    client = FakeTgClient()
    pairing = PairingStore(path=tmp_path / "pairings.json")
    adapter = TelegramAdapter(
        config=TelegramConfig(bot_token="t"),
        bindings=BindingStore(path=tmp_path / "b.json"),
        api_client=client,
        pairing=pairing,
    )
    await adapter._dispatch_update(_tg_update(42, "hello"))
    assert adapter._inbound.qsize() == 0
    assert len(client.sent) == 1
    assert "xana pair approve telegram 42" in client.sent[0][1]
    assert pairing.list_pending(platform="telegram")[0][1]["user_id"] == "42"

    # 第二条消息:仍拦截,但不再重复发提示
    await adapter._dispatch_update(_tg_update(42, "still here"))
    assert len(client.sent) == 1
    assert adapter._inbound.qsize() == 0


@pytest.mark.asyncio
async def test_telegram_paired_user_passes(tmp_path: Path) -> None:
    pairing = PairingStore(path=tmp_path / "pairings.json")
    pairing.allow("telegram", "42")
    adapter = TelegramAdapter(
        config=TelegramConfig(bot_token="t"),
        bindings=BindingStore(path=tmp_path / "b.json"),
        api_client=FakeTgClient(),
        pairing=pairing,
    )
    await adapter._dispatch_update(_tg_update(42, "hello"))
    assert adapter._inbound.qsize() == 1


@pytest.mark.asyncio
async def test_telegram_no_pairing_store_keeps_old_behavior(tmp_path: Path) -> None:
    adapter = TelegramAdapter(
        config=TelegramConfig(bot_token="t"),
        bindings=BindingStore(path=tmp_path / "b.json"),
        api_client=FakeTgClient(),
    )
    await adapter._dispatch_update(_tg_update(42, "hello"))
    assert adapter._inbound.qsize() == 1


@pytest.mark.asyncio
async def test_approve_unblocks_next_message(tmp_path: Path) -> None:
    pairing = PairingStore(path=tmp_path / "pairings.json")
    adapter = TelegramAdapter(
        config=TelegramConfig(bot_token="t"),
        bindings=BindingStore(path=tmp_path / "b.json"),
        api_client=FakeTgClient(),
        pairing=pairing,
    )
    await adapter._dispatch_update(_tg_update(42, "hello"))
    assert adapter._inbound.qsize() == 0
    pairing.approve("telegram", "42")
    await adapter._dispatch_update(_tg_update(42, "hello again"))
    assert adapter._inbound.qsize() == 1


@pytest.mark.asyncio
async def test_feishu_unpaired_gets_card_hint(tmp_path: Path) -> None:
    sender = FakeFeishuSender()
    pairing = PairingStore(path=tmp_path / "pairings.json")
    adapter = FeishuAdapter(
        config=FeishuConfig(app_id="a", app_secret="s", verification_token="v"),
        bindings=BindingStore(path=tmp_path / "b.json"),
        api_sender=sender,
        pairing=pairing,
    )
    await adapter._on_event(_feishu_event("ou_new", "oc_chat", "hi"))
    assert adapter._inbound.qsize() == 0
    assert len(sender.cards) == 1
    assert sender.cards[0][0] == "oc_chat"
    assert "xana pair approve feishu ou_new" in str(sender.cards[0][1])
    assert pairing.list_pending(platform="feishu")[0][1]["user_id"] == "ou_new"


@pytest.mark.asyncio
async def test_feishu_paired_user_passes(tmp_path: Path) -> None:
    pairing = PairingStore(path=tmp_path / "pairings.json")
    pairing.allow("feishu", "ou_admin")
    adapter = FeishuAdapter(
        config=FeishuConfig(app_id="a", app_secret="s", verification_token="v"),
        bindings=BindingStore(path=tmp_path / "b.json"),
        api_sender=FakeFeishuSender(),
        pairing=pairing,
    )
    await adapter._on_event(_feishu_event("ou_admin", "oc_chat", "hi"))
    assert adapter._inbound.qsize() == 1
```

(注意:`_feishu_event` 的具体字段结构以 `src/xana/agent_adapter/channels/feishu_events.py` 的 `parse_message_event` 实际解析为准——实现者先读该文件,如字段名不同,按真实解析器调整测试 fixture,不要改解析器。)

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_adapter_pairing_gate.py -v`
Expected: FAIL(TypeError: unexpected keyword argument 'pairing')

- [x] **Step 3: 实现门禁**

`telegram.py`:构造函数加 `pairing: PairingStore | None = None`(import `from xana.agent_adapter.channels._pairing import PairingStore`),存 `self._pairing = pairing`。`_dispatch_update` 在 allowed_user_ids 过滤之后、`allocate` 之前插入:

```python
        user_id = str(user_id_int) if user_id_int is not None else ""
        if self._pairing is not None and not self._pairing.is_allowed("telegram", user_id):
            newly_pending = self._pairing.add_pending(
                "telegram", user_id, first_message=text, chat_id=chat_id
            )
            if newly_pending and self._api_client is not None:
                await self._api_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "您尚未配对。请管理员在服务器上运行:\n"
                        f"xana pair approve telegram {user_id}"
                    ),
                )
            return
```

(下方原有 `InboundMessage(user_id=...)` 改用已计算的 `user_id or None`。)

`feishu.py`:构造函数加 `pairing: PairingStore | None = None`,`_on_event` 在 `parse_message_event` 之后、`allocate` 之前插入:

```python
        sender_id = parsed.sender_open_id or ""
        if self._pairing is not None and not self._pairing.is_allowed("feishu", sender_id):
            newly_pending = self._pairing.add_pending(
                "feishu", sender_id, first_message=parsed.text, chat_id=parsed.chat_id
            )
            if newly_pending and self._api_sender is not None:
                hint = (
                    "您尚未配对。请管理员运行: "
                    f"`xana pair approve feishu {sender_id}`"
                )
                card = _build_card(hint, [], True)
                await self._api_sender.send_card(receive_id=parsed.chat_id, card=card)
            return
```

- [x] **Step 4: 跑测试确认通过(含既有适配器测试不回归)**

Run: `python -m pytest tests/test_adapter_pairing_gate.py tests/ -q -k "telegram or feishu or pairing"`
Expected: 全部 PASS(除已知 `test_xanac_empty_input_exits_nonzero` 不在此范围)

- [x] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/channels/telegram.py src/xana/agent_adapter/channels/feishu.py tests/test_adapter_pairing_gate.py
git commit -m "feat(channels): pairing gate — unpaired users get pair-hint reply, inbound held until approve"
```

---

### Task 6: serve 读 system.json 渠道配置 + 接入 PairingStore

**Files:**
- Modify: `src/xana/cli/serve.py`
- Test: `tests/test_serve_config.py`(若已有 serve 装配测试则扩展同名文件,实现者先 `ls tests/ | grep serve` 确认)

行为:`build_adapters_from_env` 改名为 `build_adapters(*, bindings_root, env, system)`,合并两个来源——env 优先,缺失时回退 `system.channels`。两个适配器都注入共享的 `PairingStore`。注意 system.json 中 feishu 没有 `verification_token` 字段时(扫码注册不产生它),仍需 env `FEISHU_VERIFICATION_TOKEN` 才能起 webhook——飞书凭 `app_id+app_secret_key` 即满足装配条件,verification_token 取 env 或 channels.feishu 的额外字段 `verification_token`(extra="allow" 可存)。

- [x] **Step 1: 写失败测试**

```python
# tests/test_serve_config.py
"""serve adapter assembly: system.json channels + env override + pairing wiring."""
from pathlib import Path

from xana.cli.serve import build_adapters
from xana.config.system_model import SystemConfig


def _system(**channels) -> SystemConfig:
    return SystemConfig(channels=channels)


def test_telegram_from_system_json(tmp_path: Path) -> None:
    system = _system(telegram={"bot_token": "1:sys-token", "allowed_user_ids": []})
    adapters = build_adapters(bindings_root=tmp_path, env={}, system=system)
    assert [a.adapter_id for a in adapters] == ["telegram"]
    assert adapters[0]._config.bot_token == "1:sys-token"
    assert adapters[0]._pairing is not None


def test_env_overrides_system_json(tmp_path: Path) -> None:
    system = _system(telegram={"bot_token": "1:sys-token"})
    adapters = build_adapters(
        bindings_root=tmp_path, env={"TELEGRAM_BOT_TOKEN": "2:env-token"}, system=system
    )
    assert adapters[0]._config.bot_token == "2:env-token"


def test_feishu_from_system_json_with_env_verification_token(tmp_path: Path) -> None:
    system = _system(feishu={"app_id": "cli_x", "app_secret_key": "sec_y",
                             "verification_token": "vt-1"})
    adapters = build_adapters(bindings_root=tmp_path, env={}, system=system)
    assert [a.adapter_id for a in adapters] == ["feishu"]
    assert adapters[0]._config.app_id == "cli_x"
    assert adapters[0]._config.verification_token == "vt-1"
    assert adapters[0]._pairing is not None


def test_no_config_no_adapters(tmp_path: Path) -> None:
    assert build_adapters(bindings_root=tmp_path, env={}, system=SystemConfig()) == []
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_serve_config.py -v`
Expected: FAIL(ImportError: cannot import name 'build_adapters')

- [x] **Step 3: 改 serve.py**

```python
# 替换 build_adapters_from_env(签名与体),import 增加:
from xana.agent_adapter.channels._pairing import PairingStore
from xana.config.system_model import SystemConfig


def build_adapters(
    *, bindings_root: Path, env: dict[str, str], system: SystemConfig
) -> list[Adapter]:
    adapters: list[Adapter] = []
    pairing = PairingStore(path=bindings_root / "pairings.json")

    fs = system.channels.feishu
    fs_extra = fs.model_extra or {}
    app_id = env.get("FEISHU_APP_ID") or fs.app_id
    app_secret = env.get("FEISHU_APP_SECRET") or fs.app_secret_key
    verification_token = env.get("FEISHU_VERIFICATION_TOKEN") or str(
        fs_extra.get("verification_token") or ""
    )
    if app_id and app_secret:
        api_base = "https://open.larksuite.com" if fs.domain == "lark" else "https://open.feishu.cn"
        cfg = FeishuConfig(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=verification_token,
            encrypt_key=env.get("FEISHU_ENCRYPT_KEY") or None,
            api_base=api_base,
        )
        bindings = BindingStore(path=bindings_root / "feishu.bindings.json")
        adapters.append(FeishuAdapter(config=cfg, bindings=bindings, pairing=pairing))

    tg = system.channels.telegram
    bot_token = env.get("TELEGRAM_BOT_TOKEN") or tg.bot_token
    if bot_token:
        env_allowed = env.get("TELEGRAM_ALLOWED_USER_IDS", "")
        allowed = (
            frozenset(int(x) for x in env_allowed.split(",") if x.strip())
            if env_allowed
            else frozenset(tg.allowed_user_ids)
        )
        cfg = TelegramConfig(bot_token=bot_token, allowed_user_ids=allowed)
        bindings = BindingStore(path=bindings_root / "telegram.bindings.json")
        adapters.append(TelegramAdapter(config=cfg, bindings=bindings, pairing=pairing))
    return adapters
```

`_run()` 中改装配调用(boot 之后已有 `DefaultConfigProvider` 加载过 system config;serve 自己再读一次最简单):

```python
    from xana.config.loader import DefaultConfigProvider
    cp = DefaultConfigProvider()
    await cp.load(project_root)
    system = cp.load_system_config()
    adapters = build_adapters(bindings_root=bindings_root, env=dict(os.environ), system=system)
```

同时更新模块 docstring(说明读取顺序 env > system.json channels)。`bindings_root` 维持 `Path.home() / ".xana" / "channels"`,但为尊重 `XANA_HOME`,改为复用 `pair.py` 的 `pairings_path().parent`:

```python
    from xana.cli.commands.pair import pairings_path
    bindings_root = pairings_path().parent
```

- [x] **Step 4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_serve_config.py -v && python -m pytest tests/ -q`
Expected: test_serve_config 全 PASS;全量仅 `test_xanac_empty_input_exits_nonzero` 1 个已知失败

- [x] **Step 5: Commit**

```bash
git add src/xana/cli/serve.py tests/test_serve_config.py
git commit -m "feat(cli): serve reads channels from system.json (env wins) and wires PairingStore"
```

---

### Task 7: 手动冒烟(交付给用户,不阻塞)

- [x] 1. `xana setup` → 走向导:配置模型;选 2 配 Telegram token(留空 allowed ids)。
- [x] 2. `xana serve` → 用自己的 TG 账号发 "hello" → 应收到 `xana pair approve telegram <id>` 提示且无模型回复。
- [x] 3. 另开终端 `xana pair list` 看到 pending;`xana pair approve telegram <id>`。
- [x] 4. 再发一条消息 → 正常流式回复(serve 无需重启,热重载生效)。
- [x] 5. (可选,需要飞书账号)`xana setup` 选 1 扫码;`cat ~/.xana/config/system.json` 确认 app_id/app_secret_key/open_id 写入,`xana pair list` 确认管理员已 self-pair;配 `FEISHU_VERIFICATION_TOKEN` + ngrok 后 `xana serve` 冒烟。
