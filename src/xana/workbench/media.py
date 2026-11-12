"""Session-owned upload handles; browsers never submit server file paths."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from aiohttp import web

from xana.llm_driver.interfaces import ModelRegistry
from xana.multimodal.contracts import MediaInput
from xana.multimodal.files import FileMediaStore, MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, detect_format
from xana.session_config.interfaces import SessionConfigStore


class MediaUploads:
    def __init__(self, *, store: FileMediaStore, configs: SessionConfigStore,
                 models: ModelRegistry, provider: str, default_model: str) -> None:
        self._store, self._configs, self._models = store, configs, models
        self._provider, self._default_model = provider, default_model
        self._root = store.root / "uploads"
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def modalities(self, sid: str) -> tuple[str, ...]:
        client = await self._models.get(self._provider)
        if client is None:
            raise RuntimeError("model client required")
        config = await self._configs.get(sid)
        return (await client.capabilities(config.model or self._default_model)).input_modalities

    async def upload(self, sid: str, request: web.Request) -> dict:
        allowed = await self.modalities(sid)
        if not allowed:
            raise ValueError("当前模型没有开启图片或视频输入")
        fd, filename = tempfile.mkstemp(dir=self._root)
        path = Path(filename)
        try:
            size, limit, prefix = 0, MAX_VIDEO_BYTES, b""
            with os.fdopen(fd, "wb") as output:
                async for chunk in request.content.iter_chunked(65536):
                    size += len(chunk)
                    if len(prefix) < 32:
                        prefix = (prefix + chunk)[:32]
                        if len(prefix) >= 12:
                            fmt = detect_format(prefix)
                            if fmt is None:
                                raise ValueError("不支持的媒体格式")
                            kind = "video" if fmt == "mp4" else "image"
                            if kind not in allowed:
                                raise ValueError(f"当前模型没有开启 {kind} 输入")
                            limit = MAX_VIDEO_BYTES if kind == "video" else MAX_IMAGE_BYTES
                    if size > limit:
                        raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=size)
                    output.write(chunk)
            media = await asyncio.to_thread(self._store.import_file, path)
            if media.kind not in allowed:
                raise ValueError("当前模型不支持该媒体输入")
            ident = uuid4().hex
            manifest = self._root / f"{ident}.json"
            with manifest.open("x", encoding="utf-8") as output:
                json.dump({"session_id": sid, "media": asdict(media)}, output)
                output.flush()
                os.fsync(output.fileno())
            return {"upload_id": ident, "kind": media.kind, "mime_type": media.mime_type, "size_bytes": media.size_bytes}
        finally:
            path.unlink(missing_ok=True)

    def attachment(self, sid: str, ident: str) -> dict[str, str]:
        if not isinstance(ident, str) or len(ident) != 32 or any(char not in "0123456789abcdef" for char in ident):
            raise ValueError("invalid upload ID")
        path = self._root / f"{ident}.json"
        if not path.exists():
            raise LookupError("upload not found")
        data = json.loads(path.read_text())
        if data["session_id"] != sid:
            raise LookupError("upload not found")
        media = MediaInput(**data["media"])
        return {"file_path": media.path}
