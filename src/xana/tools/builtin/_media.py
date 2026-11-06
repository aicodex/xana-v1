"""出站媒体的文件校验与探测(仅图片/视频,不提供任意文件发送)。

安全边界:只接受 workspace 内、后缀与 magic bytes 一致、大小受限的普通文件;
符号链接在 resolve() 后必须仍在 workspace 内。ffmpeg/ffprobe 只做元数据与封面,不经 shell。
"""
from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from xana.agent_adapter.contracts import MediaKind

MAX_IMAGE_MB = 10
MAX_VIDEO_MB = 30
MAX_IMAGE_BYTES = MAX_IMAGE_MB * 1024 * 1024
MAX_VIDEO_BYTES = MAX_VIDEO_MB * 1024 * 1024

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_VIDEO_SUFFIXES = frozenset({".mp4"})
_CONTENT_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "mp4": "video/mp4",
}

MediaFileFailureKind = Literal["validation", "not_found", "policy_forbidden"]


class MediaFileError(Exception):
    def __init__(self, message: str, *, failure_kind: MediaFileFailureKind = "validation") -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


@dataclass(frozen=True)
class ReadMediaFile:
    path: Path
    kind: MediaKind
    filename: str
    data: bytes
    content_type: str
    format_name: str


def read_workspace_media(raw_path: str, cwd: str) -> ReadMediaFile:
    """解析并校验 workspace 内的图片/视频,返回 bytes 与元数据。"""
    raw = str(raw_path)
    if not raw.strip():
        raise MediaFileError("未发送:file_path 不能为空")
    if "\x00" in raw:
        raise MediaFileError("未发送:路径包含非法字符")

    root = Path(cwd).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise MediaFileError(f"未发送:文件不存在:{raw}", failure_kind="not_found") from exc
    except OSError as exc:
        raise MediaFileError(f"未发送:无法解析路径:{exc}") from exc

    if resolved == root or not resolved.is_relative_to(root):
        raise MediaFileError(f"未发送:路径不在当前工作区内:{raw}", failure_kind="policy_forbidden")

    suffix = resolved.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        kind: MediaKind = "image"
        limit = MAX_IMAGE_BYTES
        limit_mb = MAX_IMAGE_MB
    elif suffix in _VIDEO_SUFFIXES:
        kind = "video"
        limit = MAX_VIDEO_BYTES
        limit_mb = MAX_VIDEO_MB
    else:
        raise MediaFileError(f"未发送:只支持图片(png/jpg/jpeg/webp/gif)和 MP4 视频,不支持 {suffix or '无后缀'}")
    label = "图片" if kind == "image" else "视频"

    try:
        pre = resolved.stat()
    except FileNotFoundError as exc:
        raise MediaFileError(f"未发送:文件不存在:{raw}", failure_kind="not_found") from exc
    except OSError as exc:
        raise MediaFileError(f"未发送:无法读取文件属性:{exc}") from exc
    if not stat.S_ISREG(pre.st_mode):
        raise MediaFileError(f"未发送:不是普通文件:{raw}")
    if pre.st_size == 0:
        raise MediaFileError(f"未发送:文件为空:{raw}")
    if pre.st_size > limit:
        raise MediaFileError(f"未发送:{label}超过大小上限({limit_mb}MB)")

    try:
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise MediaFileError(f"未发送:文件不存在:{raw}", failure_kind="not_found") from exc
    except OSError as exc:
        raise MediaFileError(f"未发送:无法打开文件:{exc}") from exc

    try:
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if (st.st_dev, st.st_ino) != (pre.st_dev, pre.st_ino):
                raise MediaFileError("未发送:文件在读取前被替换,已拒绝")
            if not stat.S_ISREG(st.st_mode):
                raise MediaFileError(f"未发送:不是普通文件:{raw}")
            if st.st_size == 0:
                raise MediaFileError(f"未发送:文件为空:{raw}")
            if st.st_size > limit:
                raise MediaFileError(f"未发送:{label}超过大小上限({limit_mb}MB)")
            data = f.read(limit + 1)
    finally:
        # fdopen 正常路径已关闭;fdopen 自身失败时兜底。
        try:
            os.close(fd)
        except OSError:
            pass

    if len(data) > limit:
        raise MediaFileError(f"未发送:{label}超过大小上限({limit_mb}MB)")

    fmt = _detect_format(data)
    if kind == "image":
        expected = {_FORMAT_BY_SUFFIX[suffix]}
        if fmt not in expected:
            raise MediaFileError(f"未发送:文件内容与后缀不一致({suffix})")
    elif fmt != "mp4":
        raise MediaFileError("未发送:文件不是有效的 MP4 视频")

    return ReadMediaFile(
        path=resolved,
        kind=kind,
        filename=_clean_filename(resolved.name),
        data=data,
        content_type=_CONTENT_TYPES[fmt],
        format_name=fmt,
    )


_FORMAT_BY_SUFFIX = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
    ".mp4": "mp4",
}


def _detect_format(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] != b"qt  ":
        return "mp4"
    return None


def _clean_filename(name: str) -> str:
    cleaned = "".join(ch for ch in name if ord(ch) >= 32 and ch != "\x7f")
    if not cleaned:
        return "media"
    if len(cleaned) <= 120:
        return cleaned
    suffix = Path(cleaned).suffix
    keep = max(1, 120 - len(suffix))
    return cleaned[:keep] + suffix


async def probe_video_duration_ms(path: Path, *, timeout_s: float = 10.0) -> int | None:
    """ffprobe 取时长;失败/超时返回 None(调用方仍可发送,只是不带时长)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    try:
        seconds = float(out.decode("utf-8", "replace").strip())
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return int(seconds * 1000)


async def extract_video_cover_jpeg(
    path: Path, *, timeout_s: float = 20.0, max_bytes: int = MAX_IMAGE_BYTES,
) -> bytes | None:
    """抽首帧作封面(缩到宽 ≤1280 的 JPEG);失败/超时/过大返回 None,不影响视频发送。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-i", str(path),
            "-frames:v", "1", "-vf", "scale=min(1280\\,iw):-2",
            "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0 or not out or len(out) > max_bytes:
        return None
    if not out.startswith(b"\xff\xd8\xff"):
        return None
    return out
