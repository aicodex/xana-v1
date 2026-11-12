"""HttpxTelegramClient: thin Bot API HTTP wrapper."""
from __future__ import annotations

import logging

import httpx

_logger = logging.getLogger(__name__)

from xana.agent_adapter.channels.telegram import TelegramApiClient, TelegramConfig
from xana.agent_adapter.contracts import OutboundMedia, MediaSendError


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

    async def send_media(self, *, chat_id: str, media: OutboundMedia) -> int:
        field='video' if media.kind=='video' else 'animation' if media.content_type=='image/gif' else 'photo'
        method={'video':'sendVideo','animation':'sendAnimation','photo':'sendPhoto'}[field]
        body={'chat_id':chat_id}
        files={field:(media.filename,media.data,media.content_type)}
        if field=='video':
            body['supports_streaming']='true'
            if media.duration_ms:
                body['duration']=str(max(1,round(media.duration_ms/1000)))
            if media.cover:
                if media.cover_content_type and media.cover_content_type!='image/jpeg':
                    raise MediaSendError('Telegram 视频缩略图必须是 JPEG')
                files['thumbnail']=(media.cover_filename or 'cover.jpg',media.cover,media.cover_content_type or 'image/jpeg')
        try:
            response=await self._client.post('/'+method,data=body,files=files,timeout=120)
        except httpx.TransportError as exc:
            raise MediaSendError('Telegram 媒体上传网络错误',retryable=True) from exc
        if response.status_code>=400:
            raise MediaSendError(f'Telegram 媒体上传失败: HTTP {response.status_code}',retryable=response.status_code==429 or response.status_code>=500)
        data=response.json()
        if not data.get('ok'):
            raise MediaSendError('Telegram 媒体上传失败: '+str(data.get('description','未知错误'))[:300])
        return int(data['result']['message_id'])

    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
        reply_quote: str | None = None,
    ) -> int:
        body: dict = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode:
            body["parse_mode"] = parse_mode
        if reply_markup:
            body["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            rp: dict = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
            if reply_quote:
                # 客户端引用框只显示前 100 字(避免长消息引用框压屏幕)。
                # quote 上限 1024 字,quote_position=0 从原消息头开始截。
                rp["quote"] = reply_quote[:100]
                rp["quote_position"] = 0
            body["reply_parameters"] = rp
        resp = await self._client.post("/sendMessage", json=body)
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
        reply_markup: dict | None = None,
    ) -> None:
        resp = await self._client.post(
            "/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "link_preview_options": {"is_disabled": True},
                **({"parse_mode": parse_mode} if parse_mode else {}),
                **({"reply_markup": reply_markup} if reply_markup else {}),
            },
        )
        # Telegram answers 400 when the new text equals the current text;
        # that's a no-op for us, not an error.
        if resp.status_code == 400:
            desc = ""
            try:
                desc = resp.json().get("description") or ""
            except Exception:
                desc = resp.text[:300]
            if "message is not modified" in desc:
                return
            raise RuntimeError(f"Telegram editMessageText 400: {desc}")
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram editMessageText error: {data.get('description')}")

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None
    ) -> None:
        """Ack a button tap so the client stops the spinner. Best-effort:
        failures are swallowed at debug level (stale/expired ids are common)."""
        try:
            resp = await self._client.post(
                "/answerCallbackQuery",
                json={
                    "callback_query_id": callback_query_id,
                    **({"text": text} if text else {}),
                },
            )
            resp.raise_for_status()
        except Exception as exc:
            _logger.debug(
                "telegram: answerCallbackQuery failed (%s)", type(exc).__name__
            )

    async def set_my_commands(self, commands: list[dict]) -> bool:
        """setMyCommands:把命令清单推给 Telegram,显示在输入框左侧「/」菜单。
        每项 {command(无前导/,1-32 字符 [a-z0-9_]), description(1-256)};
        Telegram 上限 100。失败只记日志返回 False(菜单缺失不影响功能)。"""
        cleaned: list[dict] = []
        seen: set[str] = set()
        for c in commands[:100]:
            name = str(c.get("command", "")).lstrip("/").strip().lower()
            desc = str(c.get("description", "")).strip() or name
            if not name or name in seen or len(name) > 32:
                continue
            if not all(ch.isalnum() or ch == "_" for ch in name):
                continue
            seen.add(name)
            cleaned.append({"command": name, "description": desc[:256]})
        try:
            resp = await self._client.post("/setMyCommands", json={"commands": cleaned})
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            _logger.warning("telegram: setMyCommands 失败: %s", exc)
            return False
        if not data.get("ok"):
            _logger.warning("telegram: setMyCommands not ok: %s", data.get("description"))
            return False
        return True

    async def aclose(self) -> None:
        await self._client.aclose()

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> None:
        """无 webhook 时也安全调用:它会清服务端 long-poll 锁 + 可选丢 pending,
        快速恢复 sync 重启之后的 409 Conflict 状态。"""
        resp = await self._client.post(
            "/deleteWebhook",
            json={"drop_pending_updates": drop_pending_updates},
        )
        resp.raise_for_status()
        data = resp.json()
        _logger.warning(
            "telegram: deleteWebhook ok=%s desc=%s",
            data.get("ok"), data.get("description"))

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
