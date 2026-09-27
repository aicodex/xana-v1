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


def _default_post_form(url: str, body: dict) -> dict:
    """POST form-encoded; tolerates 4xx with JSON body (e.g. authorization_pending)."""
    import httpx
    resp = httpx.post(url, data=body, timeout=15.0)
    if resp.is_error:
        try:
            return resp.json()
        except Exception:
            resp.raise_for_status()
    return resp.json()


def _default_post_json(url: str, body: dict) -> dict:
    """POST JSON; raises on any HTTP error."""
    import httpx
    resp = httpx.post(url, json=body, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _default_get(url: str, headers: dict) -> dict:
    import httpx
    resp = httpx.get(url, headers=headers, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def begin_registration(*, domain: str = "feishu", post_json: PostJson = _default_post_form) -> dict:
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
    post_json: PostJson = _default_post_form,
) -> Optional[dict]:
    """Poll until scanned. Returns {app_id, app_secret, domain, open_id} or None."""
    current_domain = domain
    deadline = time.monotonic() + expire_in
    while time.monotonic() <= deadline:
        url = f"{_ACCOUNTS_BASE[current_domain]}/oauth/v1/app/registration"
        res = post_json(url, {"action": "poll", "device_code": device_code, "tp": "ob_app"})
        user_info = res.get("user_info") or {}
        # Mid-poll domain switch: Lark tenant detected
        if user_info.get("tenant_brand") == "lark" and current_domain != "lark":
            current_domain = "lark"
        if res.get("client_id") and res.get("client_secret"):
            return {
                "app_id": res["client_id"],
                "app_secret": res["client_secret"],
                "domain": current_domain,
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
    post_json: PostJson = _default_post_json,
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
    bot = info.get("bot") or (info.get("data") or {}).get("bot") or {}
    # 飞书 bot/v3/info 的机器人名字段是 app_name
    return {"bot_name": bot.get("app_name"), "bot_open_id": bot.get("open_id")}


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
    post_form: Optional[PostJson] = None,
    post_json: Optional[PostJson] = None,
    get_json: Optional[GetJson] = None,
) -> Optional[dict]:
    """Full flow. Returns {app_id, app_secret, domain, open_id, bot_name, bot_open_id} or None."""
    # post_form used for registration (form-encoded, 4xx-tolerant)
    # Falls back to post_json for backward compat with existing tests
    _post_form = post_form or post_json or _default_post_form
    _post_json = post_json or _default_post_json
    _get_json = get_json or _default_get

    begin = begin_registration(domain=initial_domain, post_json=_post_form)
    if render_qr(begin["qr_url"]):
        print(f"扫描上方二维码,或打开:\n{begin['qr_url']}")
    else:
        print(f"打开此链接完成授权:\n{begin['qr_url']}\n提示:`pip install qrcode` 可在终端直接显示二维码。")
    result = poll_registration(
        device_code=begin["device_code"],
        interval=begin["interval"],
        expire_in=min(begin["expire_in"], timeout_seconds),
        domain=initial_domain,
        post_json=_post_form,
    )
    if result is None:
        return None
    result.update(probe_bot(result["app_id"], result["app_secret"], result["domain"],
                            post_json=_post_json, get_json=_get_json))
    return result
