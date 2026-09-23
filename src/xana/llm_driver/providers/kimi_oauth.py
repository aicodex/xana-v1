"""Kimi OAuth(device code flow)+ token 存储。

协议参考 kimi-code(packages/oauth):全 POST form-urlencoded 到 OAuth host,
只传 client_id(无 scope)。端点:
  POST /api/oauth/device_authorization  → user_code/device_code/verification_uri_complete/interval
  POST /api/oauth/token(grant_type=urn:ietf:params:oauth:grant-type:device_code) 轮询,
    处理 authorization_pending / slow_down / expired_token / access_denied
  POST /api/oauth/token(grant_type=refresh_token) 刷新

token 存 $XANA_HOME/credentials/kimi.json(默认 ~/.xana/credentials/kimi.json),
0600,tmp+replace 原子写;损坏 JSON 静默视为未登录。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"

# region → (oauth_host, api_base)
REGIONS: dict[str, tuple[str, str]] = {
    "mainland-cn": ("https://auth.kimi.com", "https://api.kimi.com/coding/v1"),
    "global": ("https://auth.kimi.ai", "https://api.kimi.ai/coding/v1"),
}
_DEFAULT_REGION = "mainland-cn"

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_EXPIRY_MARGIN_S = 60.0
_HTTP_TIMEOUT_S = 30.0


class KimiAuthError(Exception):
    """未登录 / 登录过期且刷新失败。消息里引导 `xana login`。"""


def resolve_region(region: str | None = None) -> str:
    """显式参数 > $XANA_KIMI_REGION > 默认 mainland-cn。"""
    r = region or os.environ.get("XANA_KIMI_REGION") or _DEFAULT_REGION
    if r not in REGIONS:
        raise ValueError(f"unknown kimi region {r!r}; known: {sorted(REGIONS)}")
    return r


def oauth_host(region: str) -> str:
    return os.environ.get("XANA_KIMI_OAUTH_HOST") or REGIONS[region][0]


def api_base(region: str) -> str:
    return os.environ.get("XANA_KIMI_API_BASE") or REGIONS[region][1]


def credentials_path() -> Path:
    home = os.environ.get("XANA_HOME")
    root = Path(home).expanduser() if home else Path.home() / ".xana"
    return root / "credentials" / "kimi.json"


@dataclass(frozen=True)
class KimiTokens:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch 秒
    region: str = _DEFAULT_REGION


async def device_authorization(
    region: str, *, http_client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """→ {user_code, device_code, verification_uri, verification_uri_complete, interval, expires_in}"""
    return await _post_form(
        f"{oauth_host(region)}/api/oauth/device_authorization",
        {"client_id": CLIENT_ID},
        http_client=http_client,
    )


async def poll_device_token(
    region: str,
    device_code: str,
    *,
    interval: float = 5.0,
    timeout_s: float = 900.0,
    http_client: httpx.AsyncClient | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """轮询 device token → {access_token, refresh_token, expires_in, ...}。

    authorization_pending 继续等;slow_down 间隔 +5s;
    expired_token / access_denied / 超时 → KimiAuthError。
    """
    url = f"{oauth_host(region)}/api/oauth/token"
    deadline = time.monotonic() + timeout_s
    wait = max(1.0, interval)
    while True:
        try:
            return await _post_form(
                url,
                {"client_id": CLIENT_ID, "device_code": device_code,
                 "grant_type": _DEVICE_GRANT},
                http_client=http_client,
            )
        except _OAuthError as exc:
            if exc.error == "authorization_pending":
                pass
            elif exc.error == "slow_down":
                wait += 5.0
            else:
                raise KimiAuthError(
                    f"kimi 设备授权失败({exc.error});请重新运行 `xana login`"
                ) from exc
        except httpx.RequestError:
            # 轮询期间的网络抖动(DNS/连接失败)不该崩掉登录 — 在截止时间前继续等
            pass
        if time.monotonic() + wait > deadline:
            raise KimiAuthError("kimi 设备授权超时;请重新运行 `xana login`")
        await sleeper(wait)


async def refresh_tokens(
    region: str, refresh_token: str, *, http_client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """grant_type=refresh_token → 新的 {access_token, refresh_token, expires_in}。
    invalid_grant / 401 / 403 → KimiAuthError(需重新登录)。"""
    try:
        return await _post_form(
            f"{oauth_host(region)}/api/oauth/token",
            {"client_id": CLIENT_ID, "refresh_token": refresh_token,
             "grant_type": "refresh_token"},
            http_client=http_client,
        )
    except _OAuthError as exc:
        raise KimiAuthError(
            f"kimi token 刷新失败({exc.error});请重新运行 `xana login`"
        ) from exc
    except httpx.RequestError as exc:
        raise KimiAuthError(f"kimi token 刷新网络错误({exc});稍后自动重试") from exc


async def fetch_models(
    region: str, access_token: str, *, http_client: httpx.AsyncClient | None = None
) -> list[dict[str, Any]]:
    """GET {api_base}/models → data[]。每项含 id/context_length/supports_reasoning 等。"""
    owns = http_client is None
    http = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
    try:
        resp = await http.get(
            f"{api_base(region).rstrip('/')}/models",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code >= 400:
            raise KimiAuthError(
                f"拉取 kimi 模型列表失败(HTTP {resp.status_code});如 token 过期请重新 `xana login`"
            )
        data = resp.json()
        return list(data.get("data") or [])
    finally:
        if owns:
            await http.aclose()


class _OAuthError(Exception):
    def __init__(self, error: str, description: str = "") -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error


async def _post_form(
    url: str, form: dict[str, str], *, http_client: httpx.AsyncClient | None
) -> dict[str, Any]:
    owns = http_client is None
    http = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
    try:
        resp = await http.post(url, data=form)
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            payload = {}
        if resp.status_code >= 400:
            raise _OAuthError(
                str(payload.get("error") or f"http_{resp.status_code}"),
                str(payload.get("error_description") or resp.text[:200]),
            )
        if not isinstance(payload, dict):
            raise _OAuthError("bad_response", "non-object JSON")
        return payload
    finally:
        if owns:
            await http.aclose()


RefresherFn = Callable[[str, str], Awaitable[dict[str, Any]]]


class KimiTokenStore:
    """~/.xana/credentials/kimi.json 的读写 + 到期刷新。

    refresher(region, refresh_token) → token payload;默认 refresh_tokens。
    测试注入假的 refresher / sleeper 即可,不需要真实网络。
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        refresher: RefresherFn | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path or credentials_path()
        self._refresher = refresher or refresh_tokens
        self._clock = clock
        self._cached: KimiTokens | None = None

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> KimiTokens | None:
        if self._cached is not None:
            return self._cached
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            tokens = KimiTokens(
                access_token=str(raw["access_token"]),
                refresh_token=str(raw["refresh_token"]),
                expires_at=float(raw["expires_at"]),
                region=str(raw.get("region") or _DEFAULT_REGION),
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        if not tokens.access_token:
            return None
        self._cached = tokens
        return tokens

    def save(self, tokens: KimiTokens) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
            "region": tokens.region,
        }
        tmp = self._path.parent / (self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)
        try:
            self._path.chmod(0o600)
        except OSError:
            pass
        self._cached = tokens

    def save_payload(self, region: str, payload: dict[str, Any]) -> KimiTokens:
        """OAuth token 响应 → 持久化。refresh_token 缺失时保留旧值。"""
        old = self.load()
        refresh = str(payload.get("refresh_token") or (old.refresh_token if old else ""))
        tokens = KimiTokens(
            access_token=str(payload["access_token"]),
            refresh_token=refresh,
            expires_at=self._clock() + float(payload.get("expires_in") or 3600),
            region=region,
        )
        self.save(tokens)
        return tokens

    def clear(self) -> None:
        self._cached = None
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass

    async def access_token(self, *, force_refresh: bool = False) -> str:
        """取可用 access_token;到期(或 force)先刷新。未登录 → KimiAuthError。"""
        tokens = self.load()
        if tokens is None:
            raise KimiAuthError("未登录 kimi;请先运行 `xana login`")
        if not force_refresh and self._clock() < tokens.expires_at - _EXPIRY_MARGIN_S:
            return tokens.access_token
        if not tokens.refresh_token:
            raise KimiAuthError("kimi token 已过期且没有 refresh_token;请重新运行 `xana login`")
        payload = await self._refresher(tokens.region, tokens.refresh_token)
        return self.save_payload(tokens.region, payload).access_token
