"""Feishu OpenAPI sender (httpx).

_HttpxFeishuSender handles tenant token acquisition (cached, under asyncio.Lock),
interactive card message send/patch, and image/video upload + native media send.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from xana.agent_adapter.channels.feishu import FeishuConfig
from xana.agent_adapter.contracts import MediaSendError, OutboundMedia

_logger = logging.getLogger(__name__)

# 30MB 视频在慢速网络下 10s 不够;卡片仍用 client 的默认超时。
_UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


# ---------------------------------------------------------------------------
# httpx-based sender
# ---------------------------------------------------------------------------


class _HttpxFeishuSender:
    def __init__(
        self,
        config: FeishuConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._client = http_client or httpx.AsyncClient(base_url=config.api_base, timeout=10)

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

    def _check_response(self, resp: httpx.Response, action: str) -> dict:
        if resp.status_code >= 400:
            raise MediaSendError(
                f"飞书{action}失败:HTTP {resp.status_code} {resp.text[:300]}",
                retryable=resp.status_code == 429 or resp.status_code >= 500,
            )
        data = resp.json()
        if data.get("code", 0) != 0:
            raise MediaSendError(
                f"飞书{action}失败:{data.get('code')} {data.get('msg')}")
        return data

    @staticmethod
    def _transport_error(action: str, exc: httpx.TransportError) -> MediaSendError:
        return MediaSendError(f"飞书{action}网络错误:{exc}", retryable=True)

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

    # ------------------------------------------------------------------
    # Native media
    # ------------------------------------------------------------------

    async def upload_image(self, *, data: bytes, filename: str, content_type: str) -> str:
        token = await self._ensure_token()
        try:
            resp = await self._client.post(
                "/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": (filename, data, content_type)},
                timeout=_UPLOAD_TIMEOUT,
            )
        except httpx.TransportError as exc:
            raise self._transport_error("上传图片", exc) from exc
        payload = self._check_response(resp, "上传图片")
        image_key = (payload.get("data") or {}).get("image_key")
        if not image_key:
            raise MediaSendError("飞书上传图片响应缺少 image_key")
        return str(image_key)

    async def upload_video(
        self, *, data: bytes, filename: str, duration_ms: int | None = None,
    ) -> str:
        token = await self._ensure_token()
        form = {"file_type": "mp4", "file_name": filename}
        if duration_ms:
            form["duration"] = str(int(duration_ms))
        try:
            resp = await self._client.post(
                "/open-apis/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                data=form,
                files={"file": (filename, data, "video/mp4")},
                timeout=_UPLOAD_TIMEOUT,
            )
        except httpx.TransportError as exc:
            raise self._transport_error("上传视频", exc) from exc
        payload = self._check_response(resp, "上传视频")
        file_key = (payload.get("data") or {}).get("file_key")
        if not file_key:
            raise MediaSendError("飞书上传视频响应缺少 file_key")
        return str(file_key)

    async def _send_message(self, *, token: str, receive_id: str, msg_type: str,
                            content: dict) -> str:
        try:
            resp = await self._client.post(
                "/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": receive_id,
                    "msg_type": msg_type,
                    "content": json.dumps(content),
                },
            )
        except httpx.TransportError as exc:
            raise self._transport_error("发送消息", exc) from exc
        payload = self._check_response(resp, "发送消息")
        message_id = (payload.get("data") or {}).get("message_id")
        if not message_id:
            raise MediaSendError("飞书发送消息响应缺少 message_id")
        return str(message_id)

    async def send_image(self, *, receive_id: str, image_key: str) -> str:
        token = await self._ensure_token()
        return await self._send_message(
            token=token, receive_id=receive_id, msg_type="image",
            content={"image_key": image_key},
        )

    async def send_video(
        self, *, receive_id: str, file_key: str, image_key: str | None,
        filename: str, duration_ms: int | None,
    ) -> str:
        token = await self._ensure_token()
        content: dict = {"file_key": file_key, "file_name": filename}
        if image_key:
            content["image_key"] = image_key
        if duration_ms:
            content["duration"] = int(duration_ms)
        return await self._send_message(
            token=token, receive_id=receive_id, msg_type="media", content=content,
        )

    async def send_media(self, *, receive_id: str, media: OutboundMedia) -> str:
        if media.kind == "image":
            image_key = await self.upload_image(
                data=media.data, filename=media.filename, content_type=media.content_type)
            return await self.send_image(receive_id=receive_id, image_key=image_key)
        if media.kind == "video":
            file_key = await self.upload_video(
                data=media.data, filename=media.filename, duration_ms=media.duration_ms)
            image_key = None
            if media.cover:
                try:
                    image_key = await self.upload_image(
                        data=media.cover,
                        filename=media.cover_filename or "cover.jpg",
                        content_type=media.cover_content_type or "image/jpeg",
                    )
                except MediaSendError as exc:
                    # 封面失败不阻断视频:飞书允许 media 不带 image_key。
                    _logger.warning("飞书视频封面上传失败,改为无封面发送:%s", exc)
            return await self.send_video(
                receive_id=receive_id, file_key=file_key, image_key=image_key,
                filename=media.filename, duration_ms=media.duration_ms,
            )
        raise MediaSendError(f"不支持的媒体类型:{media.kind}", failure_kind="unsupported")


def build_real_sender(
    config: FeishuConfig,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> _HttpxFeishuSender:
    return _HttpxFeishuSender(config, http_client=http_client)
