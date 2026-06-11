"""HttpxTelegramClient: thin Bot API HTTP wrapper."""
from __future__ import annotations

import httpx

from xana.agent_adapter.channels.telegram import TelegramApiClient, TelegramConfig


class HttpxTelegramClient(TelegramApiClient):
    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=f"{config.api_base}/bot{config.bot_token}",
            timeout=httpx.Timeout(
                connect=10.0,
                read=config.poll_timeout_s + 5,
                write=10.0,
                pool=10.0,
            ),
        )

    async def send_message(
        self, *, chat_id: str, text: str, parse_mode: str | None = None
    ) -> int:
        resp = await self._client.post(
            "/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                **({"parse_mode": parse_mode} if parse_mode else {}),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendMessage error: {data.get('description')}")
        return int(data["result"]["message_id"])

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
    ) -> None:
        resp = await self._client.post(
            "/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                **({"parse_mode": parse_mode} if parse_mode else {}),
            },
        )
        # Telegram answers 400 when the new text equals the current text;
        # that's a no-op for us, not an error.
        if resp.status_code == 400:
            desc = resp.json().get("description") or ""
            if "message is not modified" in desc:
                return
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram editMessageText error: {data.get('description')}")

    async def get_updates(self, *, offset: int | None, timeout: int) -> list[dict]:
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        resp = await self._client.get("/getUpdates", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getUpdates error: {data.get('description')}")
        return list(data.get("result") or [])
