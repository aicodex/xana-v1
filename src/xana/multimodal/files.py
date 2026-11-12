"""Bounded media imports, immutable snapshots and verified reads; no provider calls."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile

from xana.multimodal.contracts import MediaInput

MAX_ATTACHMENTS = 16
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# Match Kimi Code ReadMediaFile: MAX_MEDIA_MEGABYTES << 20 (100 MiB).
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MIME_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp",
              "gif": "image/gif", "mp4": "video/mp4"}


def detect_format(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] != b"qt  ":
        return "mp4"
    return None


def read_media_bytes(media: MediaInput) -> bytes:
    if media.kind not in ("image", "video"):
        raise ValueError("无效的媒体类型")
    if media.size_bytes > (MAX_IMAGE_BYTES if media.kind == "image" else MAX_VIDEO_BYTES):
        raise ValueError("媒体超过大小限制")
    data = _bounded_read(Path(media.path), media.size_bytes)
    if len(data) != media.size_bytes or hashlib.sha256(data).hexdigest() != media.sha256:
        raise ValueError("媒体快照已改变或损坏，请重新读取原文件")
    fmt = detect_format(data)
    if fmt is None or MIME_TYPES[fmt] != media.mime_type or (fmt == "mp4") != (media.kind == "video"):
        raise ValueError("媒体格式与记录不匹配")
    return data


def _bounded_read(path: Path, limit: int) -> bytes:
    if limit <= 0 or limit > MAX_VIDEO_BYTES:
        raise ValueError("媒体大小无效或超过 100 MiB")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("媒体必须是普通文件")
        if info.st_size == 0 or info.st_size > limit:
            raise ValueError(f"媒体为空或超过 {limit // (1024 * 1024)} MiB")
        data = file.read(limit + 1)
    if len(data) > limit:
        raise ValueError("媒体在读取时超过大小限制")
    return data


class FileMediaStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def import_file(self, path: Path) -> MediaInput:
        # Bound and sniff before loading; images cannot consume the video allowance.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("媒体必须是普通文件")
            fmt = detect_format(file.read(32))
        if fmt is None:
            raise ValueError("不支持的媒体格式；支持 PNG/JPEG/WEBP/GIF 和 MP4")
        limit = MAX_VIDEO_BYTES if fmt == "mp4" else MAX_IMAGE_BYTES
        return self.save_bytes(_bounded_read(path, limit))

    def save_bytes(self, data: bytes) -> MediaInput:
        fmt = detect_format(data)
        if fmt is None:
            raise ValueError("不支持的媒体格式；支持 PNG/JPEG/WEBP/GIF 和 MP4")
        kind = "video" if fmt == "mp4" else "image"
        if not data or len(data) > (MAX_VIDEO_BYTES if kind == "video" else MAX_IMAGE_BYTES):
            raise ValueError("媒体超过大小限制（图片 10 MiB，视频 100 MiB）")
        digest = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        target = (self.root / f"{digest}.{fmt}").resolve()
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as file:
                tmp = Path(file.name)
                try:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                    os.replace(tmp, target)
                finally:
                    tmp.unlink(missing_ok=True)
        return MediaInput(kind, MIME_TYPES[fmt], str(target), digest, len(data))
