"""Kimi model capabilities and media transport, following official Kimi Code file inputs."""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import httpx

from xana.multimodal.contracts import InputModality, MediaInput
from xana.multimodal.files import read_media_bytes


class KimiMedia:
    def __init__(self, base_url: str, modalities: dict[str, tuple[InputModality, ...]],
                 http: httpx.AsyncClient | None) -> None:
        self._base = base_url.rstrip("/")
        self._http = http
        self._models = dict(modalities)
        self._videos: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def modalities(self, model: str) -> tuple[InputModality, ...]:
        # system.json models[].input_modalities is the sole capability authority.
        return self._models.get(model, ())

    async def parts(self, media: tuple[MediaInput, ...], model: str, token: str) -> list[dict]:
        supported = self.modalities(model)
        parts = []
        for item in media:
            if item.kind not in supported:
                raise ValueError(f"模型 {model} 未在配置中开启 {item.kind} 输入")
            data = await asyncio.to_thread(read_media_bytes, item)
            if item.kind == "image":
                url = f"data:{item.mime_type};base64," + base64.b64encode(data).decode("ascii")
                parts.append({"type": "image_url", "image_url": {"url": url}})
            else:
                async with self._lock:
                    url = self._videos.get(item.sha256)
                    if url is None:
                        http = self._http or httpx.AsyncClient(timeout=120)
                        try:
                            response = await http.post(self._base + "/files",
                                headers={"Authorization": f"Bearer {token}"},
                                data={"purpose": "video"}, files={"file": (Path(item.path).name, data, item.mime_type)}, timeout=120)
                            response.raise_for_status()
                            file_id = response.json().get("id")
                            if not file_id:
                                raise ValueError("Kimi 视频上传未返回文件 ID")
                            url = f"ms://{file_id}"
                            self._videos[item.sha256] = url
                        finally:
                            if self._http is None:
                                await http.aclose()
                parts.append({"type": "video_url", "video_url": {"url": url}})
        return parts
