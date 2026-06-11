"""Feishu webhook ASGI app + httpx-based API sender.

The webhook app handles:
  - url_verification challenge (type="url_verification")
  - Signature verification via X-Lark-* headers when encrypt_key is set
  - Routing im.message.receive_v1 events to the on_event callback

_HttpxFeishuSender handles tenant token acquisition (cached, under asyncio.Lock)
and interactive card message send/patch.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Awaitable

from aiohttp import web

from xana.agent_adapter.channels.feishu import FeishuConfig
from xana.agent_adapter.channels.feishu_events import FeishuVerifyError, verify_signature


# ---------------------------------------------------------------------------
# Webhook app
# ---------------------------------------------------------------------------


def build_webhook_app(
    *,
    config: FeishuConfig,
    on_event: Callable[[dict[str, Any]], Awaitable[None]],
) -> web.Application:
    app = web.Application()

    async def handle_webhook(request: web.Request) -> web.Response:
        body = await request.read()

        # Parse JSON
        try:
            payload: dict[str, Any] = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.Response(status=400, text="bad json")

        # URL verification challenge
        if payload.get("type") == "url_verification":
            token = payload.get("token", "")
            if token != config.verification_token:
                return web.Response(status=403, text="token mismatch")
            return web.json_response({"challenge": payload.get("challenge", "")})

        # HMAC signature verification (optional)
        if config.encrypt_key:
            ts = request.headers.get("X-Lark-Request-Timestamp", "")
            nonce = request.headers.get("X-Lark-Request-Nonce", "")
            sig = request.headers.get("X-Lark-Signature", "")
            try:
                verify_signature(
                    timestamp=ts,
                    nonce=nonce,
                    body=body,
                    encrypt_key=config.encrypt_key,
                    signature=sig,
                )
            except FeishuVerifyError as exc:
                return web.Response(status=403, text=str(exc))

        await on_event(payload)
        return web.json_response({"ok": True})

    app.router.add_post("/feishu/webhook", handle_webhook)
    return app


# ---------------------------------------------------------------------------
# httpx-based sender
# ---------------------------------------------------------------------------


class _HttpxFeishuSender:
    def __init__(self, config: FeishuConfig) -> None:
        self._config = config
        self._token: str | None = None
        self._lock: asyncio.Lock = asyncio.Lock()
        # Lazy import httpx to avoid hard dependency in tests that mock the sender
        import httpx
        self._client = httpx.AsyncClient(base_url=config.api_base, timeout=10)

    async def _ensure_token(self) -> str:
        async with self._lock:
            if self._token is not None:
                return self._token
            resp = await self._client.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self._config.app_id,
                    "app_secret": self._config.app_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = str(data["tenant_access_token"])
            return self._token

    async def send_card(self, *, receive_id: str, card: dict) -> str:
        token = await self._ensure_token()
        resp = await self._client.post(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data["data"]["message_id"])

    async def patch_card(self, *, message_id: str, card: dict) -> None:
        token = await self._ensure_token()
        resp = await self._client.patch(
            f"/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": json.dumps(card)},
        )
        resp.raise_for_status()


def build_real_sender(config: FeishuConfig) -> _HttpxFeishuSender:
    return _HttpxFeishuSender(config)
