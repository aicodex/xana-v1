"""Canonical media input contract. Binary content lives once in the asset store."""
from dataclasses import dataclass
from typing import Literal

InputModality = Literal["image", "video"]


@dataclass(frozen=True)
class MediaInput:
    kind: InputModality
    mime_type: str
    path: str
    sha256: str
    size_bytes: int
