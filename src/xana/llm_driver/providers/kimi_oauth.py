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
import contextlib
import fcntl
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from xana.config.paths import xana_home

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
    root = xana_home()
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
        self._refresh_lock = asyncio.Lock()   # 同进程内的并发刷新串行化

    @property
    def path(self) -> Path:
        return self._path

    def load(self, *, from_disk: bool = False) -> KimiTokens | None:
        """读凭据。内存缓存只在未到期时使用;到期或 from_disk 时重新读盘——其他进程(另一个 xana 进程、
        `xana login`)可能已经刷新并轮换了 refresh_token,用缓存里的旧值去刷会得到 invalid_grant。"""
        if not from_disk and self._cached is not None and self._fresh(self._cached):
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
        old = self.load(from_disk=True)
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
        """取可用 access_token;到期(或 force)先刷新。未登录 → KimiAuthError。

        kimi 每次刷新都会轮换 refresh_token:两个调用同时拿旧 refresh_token 去刷,后到的得到 invalid_grant 且
        之后一直失败。所以刷新前先拿进程内锁 + 凭据文件锁(跨进程),拿到后重新读盘:别人已经刷新过就直接用。"""
        tokens = self.load()
        if tokens is None:
            raise KimiAuthError("未登录 kimi;请先运行 `xana login`")
        if not force_refresh and self._fresh(tokens):
            return tokens.access_token
        stale = tokens.access_token
        async with self._refresh_lock:
            async with self._file_lock():
                tokens = self.load(from_disk=True)
                if tokens is None:
                    raise KimiAuthError("未登录 kimi;请先运行 `xana login`")
                if self._fresh(tokens) and (not force_refresh or tokens.access_token != stale):
                    return tokens.access_token   # 等锁期间已被刷新
                if not tokens.refresh_token:
                    raise KimiAuthError("kimi token 已过期且没有 refresh_token;请重新运行 `xana login`")
                payload = await self._refresher(tokens.region, tokens.refresh_token)
                return self.save_payload(tokens.region, payload).access_token

    def _fresh(self, tokens: KimiTokens) -> bool:
        return self._clock() < tokens.expires_at - _EXPIRY_MARGIN_S

    @contextlib.asynccontextmanager
    async def _file_lock(self, timeout_s: float = 30.0):
        """凭据文件旁的 .lock(fcntl.flock,跨进程互斥);非阻塞轮询,不卡事件循环。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise KimiAuthError("等待 kimi 凭据刷新锁超时")
                    await asyncio.sleep(0.05)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


async def fetch_usages(store: KimiTokenStore, *, base_url: str | None = None, timeout_s: float = 15.0) -> dict[str, Any]:
    """Kimi Code 订阅额度:GET {base}/usages(同 Kimi Code 的 managed-usage)。返回
    {limit5h | limit7d | monthTotal | monthCode: {usedRatio, resetAt}, ...};取不到抛 httpx 错误。"""
    token = await store.access_token()
    tokens = store.load()
    base = (base_url or api_base(tokens.region if tokens else _DEFAULT_REGION)).rstrip("/")
    async with httpx.AsyncClient(trust_env=False, timeout=timeout_s) as client:
        resp = await client.get(f"{base}/usages", headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    data = resp.json()
    return data.get("usages") if isinstance(data.get("usages"), dict) else data


def quota_exhausted(usages: dict[str, Any], *, threshold: float = 0.9) -> str | None:
    """5 小时 / 7 天 / 月额度的已用比例到阈值 → 返回原因(含重置时间),否则 None。
    接口字段是 snake_case(limit_5h.used_ratio / reset_time);Kimi Code 前端转成 camelCase,两种都认。"""
    for keys, label in ((("limit_5h", "limit5h"), "5 小时额度"), (("limit_7d", "limit7d"), "7 天额度"),
                        (("limit_month_total", "monthTotal"), "月额度")):
        entry = next((usages[k] for k in keys if isinstance(usages.get(k), dict)), {})
        ratio = entry.get("used_ratio", entry.get("usedRatio"))
        if isinstance(ratio, (int, float)) and ratio >= threshold:
            reset = entry.get("reset_time") or entry.get("resetAt") or "未知"
            return f"Kimi {label}已用 {ratio:.0%}(重置于 {reset})"
    return None


def quota_reset_wait(usages: dict[str, Any], *, threshold: float = 0.9,
                     now: float | None = None) -> tuple[str, float] | None:
    """已用到阈值的额度中最晚重置的那个:(原因, 距重置还有多少秒,含 60 秒余量);都没到阈值返回 None。"""
    from datetime import datetime
    now = time.time() if now is None else now
    hit: list[tuple[str, float]] = []
    for keys, label in ((("limit_5h", "limit5h"), "5 小时额度"), (("limit_7d", "limit7d"), "7 天额度"),
                        (("limit_month_total", "monthTotal"), "月额度")):
        entry = next((usages[k] for k in keys if isinstance(usages.get(k), dict)), {})
        ratio = entry.get("used_ratio", entry.get("usedRatio"))
        reset = entry.get("reset_time") or entry.get("resetAt")
        if isinstance(ratio, (int, float)) and ratio >= threshold and reset:
            try:
                at = datetime.fromisoformat(str(reset).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            hit.append((f"Kimi {label}已用 {ratio:.0%},{time.strftime('%H:%M', time.localtime(at))} 重置",
                        max(0.0, at - now) + 60.0))
    return max(hit, key=lambda h: h[1]) if hit else None
