"""Schema versioning, step-wise migration, and deterministic JSON wire encoding.

Strictly Python stdlib-only. No runtime handles, no business imports.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from typing import Any, Callable

SCHEMA_VERSION: int = 1
MIN_READER_VERSION: int = 1

MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]


class VersionError(ValueError):
    """Raised when a wire/tape/eval record cannot be read or migrated safely."""

    def __init__(self, message: str, *, found_version: int, supported_version: int) -> None:
        super().__init__(message)
        self.found_version = found_version
        self.supported_version = supported_version


def assert_reader_compatible(min_reader_version: int, *, supported: int = SCHEMA_VERSION) -> None:
    if not isinstance(min_reader_version, int) or isinstance(min_reader_version, bool) or min_reader_version < 1:
        raise VersionError(
            f"invalid min_reader_version: {min_reader_version!r}",
            found_version=0,
            supported_version=supported,
        )
    if min_reader_version > supported:
        raise VersionError(
            f"record requires reader version >= {min_reader_version}, supported <= {supported}",
            found_version=min_reader_version,
            supported_version=supported,
        )


@dataclasses.dataclass(frozen=True)
class MigrationRegistry:
    """Ordered, pure per-record migration chain: (from_version, to_version, fn)."""

    steps: tuple[tuple[int, int, MigrationFn], ...] = ()

    def migrate(self, record: dict[str, Any], *, target: int = SCHEMA_VERSION) -> dict[str, Any]:
        version = record.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise VersionError(
                f"record missing valid integer schema_version: {version!r}",
                found_version=0,
                supported_version=target,
            )
        if version > target:
            raise VersionError(
                f"record schema_version {version} exceeds target {target}",
                found_version=version,
                supported_version=target,
            )
        current = dict(record)
        by_from = {src: (dst, fn) for src, dst, fn in self.steps}
        while version < target:
            step = by_from.get(version)
            if step is None:
                raise VersionError(
                    f"no migration path from schema_version {version} to {target}",
                    found_version=version,
                    supported_version=target,
                )
            next_version, fn = step
            if next_version <= version or next_version > target:
                raise VersionError(
                    f"invalid migration step {version} -> {next_version} for target {target}",
                    found_version=version,
                    supported_version=target,
                )
            current = fn(dict(current))
            version = next_version
        return current


def to_wire(obj: Any) -> Any:
    """Convert pure protocol values/dataclasses into JSON-serializable primitives.

    Rejects NaN/Infinity, Path, frozenset, bytes, callables, and live runtime objects.
    """
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"non-finite float is not valid JSON: {obj!r}")
        return obj
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: to_wire(getattr(obj, f.name))
            for f in dataclasses.fields(obj)
        }
    if isinstance(obj, (list, tuple)):
        return [to_wire(item) for item in obj]
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"wire dict keys must be str, got {type(key).__name__}")
            result[key] = to_wire(value)
        return result
    raise TypeError(f"value of type {type(obj).__name__} is not JSON-wire-serializable")


def canonical_json(obj: Any) -> str:
    """Deterministic UTF-8 JSON encoding with sorted keys and compact separators."""
    return json.dumps(
        to_wire(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))
