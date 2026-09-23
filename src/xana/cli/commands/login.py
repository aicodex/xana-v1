"""xana login / xana logout — Kimi OAuth 设备码登录。

流程(kimi-code 同款 device flow,全程无浏览器回调依赖):
  1. POST /api/oauth/device_authorization → 显示 user_code + verification_uri_complete
  2. 轮询 /api/oauth/token 直到用户在网页确认
  3. token 原子写入 ~/.xana/credentials/kimi.json(0600)
  4. GET /models 拉取服务端模型目录,把 k3 / kimi-for-coding 登记进
     system.json models[](type="kimi",context_window 取服务端声明,默认 1M);
     若 default_model 为空则默认 k3

token 到期由 KimiTokenStore 自动刷新;refresh 失败会提示重新 `xana login`。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Awaitable, Callable, TextIO

from xana.cli.commands.setup_wizard import _load_raw, _save_raw
from xana.llm_driver.providers import kimi_oauth

_WANTED_MODELS = ("k3", "kimi-for-coding")
_DEFAULT_CONTEXT_WINDOW = 1_048_576  # 服务端未声明时按 1M 处理


class LoginFlow:
    """网络侧可注入边界 — 测试用假实现替换,不碰真实 OAuth host。"""

    async def authorize(self, region: str) -> dict[str, Any]:
        return await kimi_oauth.device_authorization(region)

    async def poll(self, region: str, device_code: str, interval: float) -> dict[str, Any]:
        return await kimi_oauth.poll_device_token(region, device_code, interval=interval)

    async def fetch_models(self, region: str, access_token: str) -> list[dict[str, Any]]:
        return await kimi_oauth.fetch_models(region, access_token)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--region",
        choices=tuple(kimi_oauth.REGIONS),
        default=None,
        help="kimi 账号区域(默认 mainland-cn;也可用 XANA_KIMI_REGION)",
    )


def add_logout_arguments(parser: argparse.ArgumentParser) -> None:
    return None


def run(args: argparse.Namespace, *, out: TextIO, flow: LoginFlow | None = None) -> int:
    region = kimi_oauth.resolve_region(getattr(args, "region", None))
    store = kimi_oauth.KimiTokenStore()
    existing = store.load()
    if existing is not None:
        print(f"[xana login] 已登录(region={existing.region})。重新登录请先 `xana logout`。", file=out)
        return 0
    try:
        return asyncio.run(_login(region, store, out, flow or LoginFlow()))
    except kimi_oauth.KimiAuthError as exc:
        print(f"[xana login] error: {exc}", file=out)
        return 1
    except KeyboardInterrupt:
        print("\n[xana login] 已取消。", file=out)
        return 130


def run_logout(args: argparse.Namespace, *, out: TextIO) -> int:  # noqa: ARG001
    store = kimi_oauth.KimiTokenStore()
    if store.load() is None and not store.path.exists():
        print("[xana logout] 未登录 kimi。", file=out)
        return 0
    store.clear()
    print(f"[xana logout] 已删除 {store.path}", file=out)
    return 0


async def _login(region: str, store: kimi_oauth.KimiTokenStore,
                 out: TextIO, flow: LoginFlow) -> int:
    auth = await flow.authorize(region)
    user_code = auth.get("user_code", "")
    url = auth.get("verification_uri_complete") or auth.get("verification_uri", "")
    print("[xana login] 请在浏览器中完成授权:", file=out)
    print(f"    {url}", file=out)
    if user_code:
        print(f"    授权码: {user_code}", file=out)
    payload = await flow.poll(
        region, str(auth.get("device_code", "")), float(auth.get("interval") or 5.0)
    )
    tokens = store.save_payload(region, payload)
    print(f"[xana login] v 登录成功,token 已写入 {store.path}(chmod 0600)", file=out)

    try:
        models = await flow.fetch_models(region, tokens.access_token)
    except kimi_oauth.KimiAuthError as exc:
        print(f"[xana login] 拉取模型列表失败:{exc}", file=out)
        print("[xana login] 登录已完成;之后可重跑 `xana login` 登记模型。", file=out)
        return 0
    registered = _register_models(models, out)
    if registered:
        print(f"[xana login] v 已登记模型: {', '.join(registered)}(type=kimi, 1M 上下文)", file=out)
    else:
        print("[xana login] 账号未见 k3 / kimi-for-coding;模型未登记。", file=out)
    return 0


def _register_models(models: list[dict[str, Any]], out: TextIO) -> list[str]:
    """把服务端目录里想要的模型写进 system.json;同名 entry 更新 context_window。"""
    wanted = {m.get("id"): m for m in models if m.get("id") in _WANTED_MODELS}
    if not wanted:
        return []
    data = _load_raw()
    entries = list(data.get("models") or [])
    registered: list[str] = []
    for model_id in _WANTED_MODELS:
        info = wanted.get(model_id)
        if info is None:
            continue
        ctx = int(info.get("context_length") or _DEFAULT_CONTEXT_WINDOW)
        entry = next((e for e in entries if e.get("name") == model_id), None)
        if entry is None:
            entries.append({"name": model_id, "type": "kimi",
                            "api_key": "", "context_window": ctx})
        else:
            entry["type"] = "kimi"
            entry["context_window"] = ctx
        registered.append(model_id)
    data["models"] = entries
    dm = dict(data.get("default_model") or {})
    if dm.get("chat") is None and registered:
        dm["chat"] = "k3" if "k3" in registered else registered[0]
        dm["agent"] = dm["agent"] if dm.get("agent") is not None else dm["chat"]
        data["default_model"] = dm
        print(f"[xana login] 默认 chat/agent 模型 → {dm['chat']}(system.json 可改)", file=out)
    _save_raw(data)
    return registered
