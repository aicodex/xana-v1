"""Feishu OpenAPI sender (httpx).

_HttpxFeishuSender handles tenant token acquisition (cached, under asyncio.Lock)
and interactive card message send/patch.
"""
from __future__ import annotations

import asyncio
import json
import time

from xana.agent_adapter.channels.feishu import FeishuConfig


# ---------------------------------------------------------------------------
# httpx-based sender
# ---------------------------------------------------------------------------


class _HttpxFeishuSender:
    def __init__(self, config: FeishuConfig) -> None:
        self._config = config
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()
        # Lazy import httpx to avoid hard dependency in tests that mock the sender
        import httpx
        self._client = httpx.AsyncClient(base_url=config.api_base, timeout=10)

    async def _ensure_token(self) -> str:
        async with self._lock:
            # Refresh if no token or within 60s of expiry
            if self._token is not None and time.monotonic() < self._token_expires_at - 60:
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
            if data.get("code", 0) != 0:
                raise RuntimeError(
                    f"Feishu token error {data.get('code')}: {data.get('msg')}"
                )
            self._token = str(data["tenant_access_token"])
            self._token_expires_at = time.monotonic() + int(data.get("expire", 7200))
            return self._token

    async def send_card(
        self, *, receive_id: str, card: dict,
        reply_message_id: str | None = None,
    ) -> str:
        token = await self._ensure_token()
        if reply_message_id:
            # Reply 端点:把卡片作为对原消息的回复发出,客户端自动显示 quote 框。
            resp = await self._client.post(
                f"/open-apis/im/v1/messages/{reply_message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "msg_type": "interactive",
                    "content": json.dumps(card),
                },
            )
        else:
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
        if data.get("code", 0) != 0:
            raise RuntimeError(
                f"Feishu send_card error {data.get('code')}: {data.get('msg')}"
            )
        return str(data["data"]["message_id"])

    async def patch_card(self, *, message_id: str, card: dict) -> None:
        token = await self._ensure_token()
        resp = await self._client.patch(
            f"/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": json.dumps(card)},
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Feishu patch_card HTTP {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        if data.get("code", 0) != 0:
            raise RuntimeError(
                f"Feishu patch_card error {data.get('code')}: {data.get('msg')}"
            )


def build_real_sender(config: FeishuConfig) -> _HttpxFeishuSender:
    return _HttpxFeishuSender(config)
