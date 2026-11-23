"""Append-only Tape protocol, content-addressed blob offloading, and reader/verifier.

Strictly Python stdlib-only. Uses explicit nested `payload` envelopes (never flattened).
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from xana.protocol.primitives import (
    BlobRef,
    CaptureStatus,
    ComponentVersion,
    Provenance,
    TokenUsage,
)
from xana.protocol.versioning import (
    MIN_READER_VERSION,
    SCHEMA_VERSION,
    assert_reader_compatible,
    canonical_json,
    sha256_bytes,
    sha256_text,
    to_wire,
)

TAPE_SCHEMA_VERSION: int = SCHEMA_VERSION
DEFAULT_BLOB_THRESHOLD_BYTES: int = 64 * 1024
DEFAULT_MAX_BLOB_BYTES: int = 64 * 1024 * 1024
DEFAULT_MAX_TAPE_BYTES: int = 256 * 1024 * 1024

TapeStatus = Literal["complete", "unclosed", "corrupted"]
TapeCloseStatus = Literal["completed", "failed", "cancelled"]

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_BLOB_REF_KEY = "__blob_ref__"


def _default_clock_ms() -> int:
    return int(time.time() * 1000)


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_sha256_hex(value: str, label: str = "blob_id") -> str:
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise ValueError(f"invalid {label}: must be 64 lowercase hex chars, got {value!r}")
    return value


def _safe_blob_path(bundle_dir: Path, sha256_hex: str) -> Path:
    clean = _validate_sha256_hex(sha256_hex)
    blobs_dir = bundle_dir / "blobs"
    if blobs_dir.is_symlink():
        raise ValueError(f"blobs directory must not be a symlink: {blobs_dir}")
    target = blobs_dir / clean
    if target.is_symlink():
        raise ValueError(f"blob path must not be a symlink: {target}")
    resolved_blobs = blobs_dir.resolve()
    resolved_target = target.resolve()
    if resolved_target.parent != resolved_blobs:
        raise ValueError(f"blob path escapes blobs directory: {target}")
    return target


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    session_id: str
    tape_id: str
    created_at_ms: int
    model: str
    provider: str
    sampling: dict[str, Any] = field(default_factory=dict)
    seed: int | None = None
    components: tuple[ComponentVersion, ...] = ()
    prompt_hashes: dict[str, str] = field(default_factory=dict)
    tool_schema_hash: str = ""
    env_digest: str = ""
    evaluator_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = TAPE_SCHEMA_VERSION

    def manifest_hash(self) -> str:
        return sha256_text(canonical_json(self))


@dataclass(frozen=True)
class TapeHeader:
    tape_id: str
    session_id: str
    run_id: str
    created_at_ms: int
    manifest_hash: str = ""
    parent_tape_id: str | None = None
    inherited_seq: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = TAPE_SCHEMA_VERSION
    min_reader_version: int = MIN_READER_VERSION
    record_type: Literal["tape.header"] = "tape.header"


@dataclass(frozen=True)
class TapeEvent:
    seq: int
    ts_ms: int
    type: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    turn_id: str | None = None
    branch_id: str | None = None
    batch_id: str | None = None
    actor: str = ""
    provenance: Provenance = "internal"
    payload: dict[str, Any] = field(default_factory=dict)
    usage: TokenUsage | None = None
    latency_ms: float | None = None
    record_type: Literal["tape.event"] = "tape.event"


@dataclass(frozen=True)
class TapeClosed:
    seq: int
    ts_ms: int
    status: TapeCloseStatus
    total_events: int
    summary: dict[str, Any] = field(default_factory=dict)
    record_type: Literal["tape.closed"] = "tape.closed"


@dataclass(frozen=True)
class TapeCaptureLimits:
    blob_threshold_bytes: int = DEFAULT_BLOB_THRESHOLD_BYTES
    max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES
    max_tape_bytes: int = DEFAULT_MAX_TAPE_BYTES
    lifecycle_reserve_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.blob_threshold_bytes <= 0 or self.max_blob_bytes <= 0 or self.max_tape_bytes <= 0:
            raise ValueError("TapeCaptureLimits byte bounds must be positive integers")
        reserve = self.effective_reserve_bytes
        if reserve < 0 or reserve > self.max_tape_bytes:
            raise ValueError(
                f"lifecycle_reserve_bytes ({reserve}) must be within [0, max_tape_bytes={self.max_tape_bytes}]"
            )

    @property
    def effective_reserve_bytes(self) -> int:
        if self.lifecycle_reserve_bytes is not None:
            return self.lifecycle_reserve_bytes
        return min(self.max_tape_bytes // 8, 1024 * 1024)


@dataclass(frozen=True)
class TapeReadResult:
    header: TapeHeader
    events: tuple[TapeEvent, ...]
    closed: TapeClosed | None
    status: TapeStatus
    errors: tuple[str, ...] = ()


def _validate_payload_wire(value: Any, blob_threshold_bytes: int) -> None:
    """Pre-validate payload tree before any blob I/O or seq mutation."""
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float is not valid JSON: {value!r}")
        return
    if isinstance(value, bytes):
        if len(value) <= blob_threshold_bytes:
            raise TypeError("raw bytes below blob_threshold_bytes are not valid JSON wire values")
        return
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            _validate_payload_wire(getattr(value, f.name), blob_threshold_bytes)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_payload_wire(item, blob_threshold_bytes)
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"wire dict keys must be str, got {type(k).__name__}")
            _validate_payload_wire(v, blob_threshold_bytes)
        return
    raise TypeError(f"value of type {type(value).__name__} is not JSON-wire-serializable")


class TapeWriter:
    """Append-only Tape bundle writer with write-before-reference CAS blob offloading."""

    def __init__(
        self,
        bundle_dir: Path,
        header: TapeHeader,
        *,
        manifest: RunManifest | None = None,
        limits: TapeCaptureLimits | None = None,
        clock_ms: Callable[[], int] = _default_clock_ms,
    ) -> None:
        self._dir = Path(bundle_dir)
        self._tape_path = self._dir / "tape.jsonl"
        if self._tape_path.exists():
            raise FileExistsError(f"tape already exists at {self._tape_path}; refusing to overwrite/corrupt")

        self._header = header
        self._limits = limits or TapeCaptureLimits()
        self._clock_ms = clock_ms
        self._seq = 0
        self._event_count = 0
        self._closed = False
        self._bytes_written = 0
        self._written_blobs: set[str] = set()

        # Pre-serialize before opening file handles so invalid inputs leak nothing
        manifest_bytes = (canonical_json(manifest) + "\n").encode("utf-8") if manifest is not None else None
        header_line = canonical_json(header) + "\n"

        self._dir.mkdir(parents=True, exist_ok=True)
        if manifest_bytes is not None:
            (self._dir / "manifest.json").write_bytes(manifest_bytes)

        self._fh = self._tape_path.open("x", encoding="utf-8", newline="\n")
        try:
            self._write_raw_line(header_line)
        except Exception:
            self._fh.close()
            self._closed = True
            raise

    def __enter__(self) -> "TapeWriter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._closed = True
        self._fh.close()

    def _write_raw_line(self, line: str) -> None:
        self._fh.write(line)
        self._fh.flush()
        self._bytes_written += len(line.encode("utf-8"))

    def _offload_bytes(self, raw: bytes, media_type: str, *, use_reserve: bool = False) -> dict[str, Any]:
        original_bytes = len(raw)
        capture: CaptureStatus = "complete"
        data = raw
        if original_bytes > self._limits.max_blob_bytes:
            data = raw[: self._limits.max_blob_bytes]
            capture = "truncated"

        digest = sha256_bytes(data)
        budget = (
            self._limits.max_tape_bytes
            if use_reserve
            else self._limits.max_tape_bytes - self._limits.effective_reserve_bytes
        )
        if digest not in self._written_blobs and (self._bytes_written + len(data) > budget):
            ref = BlobRef(
                blob_id=digest,
                sha256=digest,
                media_type=media_type,
                original_bytes=original_bytes,
                retained_bytes=0,
                capture="omitted",
            )
            return {_BLOB_REF_KEY: to_wire(ref)}

        blobs_dir = self._dir / "blobs"
        blobs_dir.mkdir(parents=True, exist_ok=True)
        target = _safe_blob_path(self._dir, digest)
        if not target.exists():
            tmp_path = blobs_dir / f".tmp.{os.getpid()}.{digest}"
            with tmp_path.open("wb") as bf:
                bf.write(data)
                bf.flush()
                os.fsync(bf.fileno())
            os.replace(tmp_path, target)
            self._bytes_written += len(data)
        self._written_blobs.add(digest)

        ref = BlobRef(
            blob_id=digest,
            sha256=digest,
            media_type=media_type,
            original_bytes=original_bytes,
            retained_bytes=len(data),
            capture=capture,
        )
        return {_BLOB_REF_KEY: to_wire(ref)}

    def _process_payload(self, value: Any, *, use_reserve: bool = False) -> Any:
        if isinstance(value, bytes):
            if len(value) > self._limits.blob_threshold_bytes:
                return self._offload_bytes(value, "application/octet-stream", use_reserve=use_reserve)
            raise TypeError("raw bytes below blob_threshold_bytes are not valid JSON wire values")
        if isinstance(value, str):
            raw = value.encode("utf-8")
            if len(raw) > self._limits.blob_threshold_bytes:
                return self._offload_bytes(raw, "text/plain; charset=utf-8", use_reserve=use_reserve)
            return value
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                f.name: self._process_payload(getattr(value, f.name), use_reserve=use_reserve)
                for f in dataclasses.fields(value)
            }
        if isinstance(value, dict):
            return {k: self._process_payload(v, use_reserve=use_reserve) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._process_payload(item, use_reserve=use_reserve) for item in value]
        return value

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None = None,
        turn_id: str | None = None,
        branch_id: str | None = None,
        batch_id: str | None = None,
        actor: str = "",
        provenance: Provenance = "internal",
        usage: TokenUsage | None = None,
        latency_ms: float | None = None,
        ts_ms: int | None = None,
        lifecycle: bool = False,
    ) -> TapeEvent:
        if self._closed:
            raise RuntimeError("cannot append to a closed TapeWriter")
        if not isinstance(payload, dict):
            raise TypeError(f"TapeEvent payload must be dict, got {type(payload).__name__}")
        # Validate full payload before any blob I/O or counter mutation
        _validate_payload_wire(payload, self._limits.blob_threshold_bytes)
        use_reserve = lifecycle or event_type.startswith("error") or event_type.endswith((".error", ".failed"))
        processed_payload = self._process_payload(payload, use_reserve=use_reserve)
        next_seq = self._seq + 1
        event = TapeEvent(
            seq=next_seq,
            ts_ms=self._clock_ms() if ts_ms is None else ts_ms,
            type=event_type,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            turn_id=turn_id,
            branch_id=branch_id,
            batch_id=batch_id,
            actor=actor,
            provenance=provenance,
            payload=processed_payload,
            usage=usage,
            latency_ms=latency_ms,
        )
        line = canonical_json(event) + "\n"
        self._write_raw_line(line)
        self._seq = next_seq
        self._event_count += 1
        return event

    def close(
        self,
        status: TapeCloseStatus,
        *,
        summary: dict[str, Any] | None = None,
        ts_ms: int | None = None,
    ) -> TapeClosed:
        if self._closed:
            raise RuntimeError("TapeWriter is already closed")
        clean_summary = dict(summary) if summary else {}
        _validate_payload_wire(clean_summary, self._limits.blob_threshold_bytes)
        processed_summary = self._process_payload(clean_summary, use_reserve=True)
        next_seq = self._seq + 1
        closed = TapeClosed(
            seq=next_seq,
            ts_ms=self._clock_ms() if ts_ms is None else ts_ms,
            status=status,
            total_events=self._event_count,
            summary=processed_summary,
        )
        line = canonical_json(closed) + "\n"
        self._write_raw_line(line)
        self._seq = next_seq
        self._closed = True
        self._fh.close()
        return closed


def read_blob(bundle_dir: Path, blob_ref: BlobRef) -> bytes:
    """Read and SHA-256 verify an offloaded blob; fails loudly on missing/corrupt/unsafe paths."""
    if blob_ref.capture == "omitted":
        raise ValueError(f"blob {blob_ref.blob_id} was omitted (original_bytes={blob_ref.original_bytes})")
    _validate_sha256_hex(blob_ref.blob_id, "blob_id")
    _validate_sha256_hex(blob_ref.sha256, "sha256")
    target = _safe_blob_path(Path(bundle_dir), blob_ref.sha256)
    if not target.is_file():
        raise FileNotFoundError(f"missing blob file: {target}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read()
    actual_sha = sha256_bytes(data)
    if actual_sha != blob_ref.sha256:
        raise ValueError(f"blob sha256 mismatch for {target}: expected {blob_ref.sha256}, got {actual_sha}")
    if len(data) != blob_ref.retained_bytes:
        raise ValueError(
            f"blob byte length mismatch for {target}: expected {blob_ref.retained_bytes}, got {len(data)}"
        )
    return data


def _collect_blob_refs(obj: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        if len(obj) == 1 and _BLOB_REF_KEY in obj and isinstance(obj[_BLOB_REF_KEY], dict):
            refs.append(obj[_BLOB_REF_KEY])
        else:
            for v in obj.values():
                refs.extend(_collect_blob_refs(v))
    elif isinstance(obj, list):
        for item in obj:
            refs.extend(_collect_blob_refs(item))
    return refs


def _parse_token_usage(raw: Any) -> TokenUsage | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"usage must be dict, got {type(raw).__name__}")
    return TokenUsage(
        input_tokens=raw.get("input_tokens", 0),
        output_tokens=raw.get("output_tokens", 0),
        cache_read_tokens=raw.get("cache_read_tokens", 0),
        cache_write_tokens=raw.get("cache_write_tokens", 0),
        reasoning_tokens=raw.get("reasoning_tokens", 0),
        total_tokens=raw.get("total_tokens"),
        cost_usd=raw.get("cost_usd"),
    )


def _split_jsonl_bytes(raw: bytes) -> list[bytes]:
    if not raw:
        return []
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return [ln[:-1] if ln.endswith(b"\r") else ln for ln in lines]


def _parse_event_obj(
    obj: dict[str, Any], line_no: int, bundle: Path, *, verify_blobs: bool, errors: list[str]
) -> TapeEvent | None:
    required = ("seq", "ts_ms", "type", "trace_id", "span_id")
    missing = [k for k in required if k not in obj]
    if missing:
        errors.append(f"line {line_no}: tape.event missing required fields {missing}")
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        errors.append(f"line {line_no}: payload must be dict")
        payload = {}
    if verify_blobs:
        for ref_raw in _collect_blob_refs(payload):
            try:
                ref = BlobRef(**ref_raw)
                if ref.capture != "omitted":
                    read_blob(bundle, ref)
            except Exception as exc:
                errors.append(f"line {line_no}: blob verification failed: {exc}")
    try:
        usage = _parse_token_usage(obj.get("usage"))
    except ValueError as exc:
        errors.append(f"line {line_no}: {exc}")
        usage = None
    return TapeEvent(
        seq=obj["seq"],
        ts_ms=obj["ts_ms"],
        type=obj["type"],
        trace_id=obj["trace_id"],
        span_id=obj["span_id"],
        parent_span_id=obj.get("parent_span_id"),
        turn_id=obj.get("turn_id"),
        branch_id=obj.get("branch_id"),
        batch_id=obj.get("batch_id"),
        actor=obj.get("actor", ""),
        provenance=obj.get("provenance", "internal"),
        payload=payload,
        usage=usage,
        latency_ms=obj.get("latency_ms"),
    )


def read_tape(bundle_dir: Path, *, verify_blobs: bool = True) -> TapeReadResult:
    """Read and validate a Tape bundle. Raises VersionError if min_reader_version > supported."""
    bundle = Path(bundle_dir)
    tape_path = bundle / "tape.jsonl"
    raw_lines = _split_jsonl_bytes(tape_path.read_bytes())
    if not raw_lines:
        raise ValueError(f"empty tape file: {tape_path}")

    header_raw = json.loads(raw_lines[0].decode("utf-8"))
    if not isinstance(header_raw, dict) or header_raw.get("record_type") != "tape.header":
        raise ValueError(f"line 1 must be tape.header object, got {header_raw!r}")
    assert_reader_compatible(header_raw.get("min_reader_version", 1), supported=TAPE_SCHEMA_VERSION)
    header = TapeHeader(
        tape_id=header_raw["tape_id"],
        session_id=header_raw["session_id"],
        run_id=header_raw["run_id"],
        created_at_ms=header_raw["created_at_ms"],
        manifest_hash=header_raw.get("manifest_hash", ""),
        parent_tape_id=header_raw.get("parent_tape_id"),
        inherited_seq=header_raw.get("inherited_seq", 0),
        metadata=header_raw.get("metadata", {}),
        schema_version=header_raw.get("schema_version", TAPE_SCHEMA_VERSION),
        min_reader_version=header_raw.get("min_reader_version", MIN_READER_VERSION),
    )

    events: list[TapeEvent] = []
    closed: TapeClosed | None = None
    errors: list[str] = []
    expected_seq = 1

    for line_no, raw_line in enumerate(raw_lines[1:], start=2):
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"line {line_no}: invalid UTF-8/JSON ({exc})")
            continue
        if not isinstance(obj, dict):
            errors.append(f"line {line_no}: expected JSON object, got {type(obj).__name__}")
            continue
        rec_type = obj.get("record_type")
        seq = obj.get("seq")
        if not _is_strict_int(seq) or seq != expected_seq:
            errors.append(f"line {line_no}: expected seq {expected_seq}, got {seq!r}")
        if _is_strict_int(seq):
            expected_seq = seq + 1

        if rec_type == "tape.event":
            if closed is not None:
                errors.append(f"line {line_no}: event appeared after tape.closed")
            parsed = _parse_event_obj(obj, line_no, bundle, verify_blobs=verify_blobs, errors=errors)
            if parsed is not None:
                events.append(parsed)
        elif rec_type == "tape.closed":
            if closed is not None:
                errors.append(f"line {line_no}: duplicate tape.closed sentinel")
                continue
            missing = [k for k in ("seq", "ts_ms", "status", "total_events") if k not in obj]
            if missing:
                errors.append(f"line {line_no}: tape.closed missing required fields {missing}")
                continue
            closed = TapeClosed(
                seq=obj["seq"],
                ts_ms=obj["ts_ms"],
                status=obj["status"],
                total_events=obj["total_events"],
                summary=obj.get("summary", {}) if isinstance(obj.get("summary", {}), dict) else {},
            )
            if closed.total_events != len(events):
                errors.append(
                    f"line {line_no}: tape.closed total_events={closed.total_events} != actual {len(events)}"
                )
        else:
            errors.append(f"line {line_no}: unknown record_type {rec_type!r}")

    status: TapeStatus = "corrupted" if errors else ("unclosed" if closed is None else "complete")
    return TapeReadResult(header=header, events=tuple(events), closed=closed, status=status, errors=tuple(errors))


def slice_tape(
    result: TapeReadResult,
    *,
    from_seq: int = 1,
    to_seq: int | None = None,
    turn_id: str | None = None,
    span_id: str | None = None,
    types: frozenset[str] | None = None,
) -> tuple[TapeEvent, ...]:
    """Filter tape events by seq range, turn/span causal coordinate, and event type."""
    selected: list[TapeEvent] = []
    for ev in result.events:
        if ev.seq < from_seq:
            continue
        if to_seq is not None and ev.seq > to_seq:
            continue
        if turn_id is not None and ev.turn_id != turn_id:
            continue
        if span_id is not None and ev.span_id != span_id and ev.parent_span_id != span_id:
            continue
        if types is not None and ev.type not in types:
            continue
        selected.append(ev)
    return tuple(selected)


def redact_tape_events(events: tuple[TapeEvent, ...] | list[TapeEvent]) -> list[dict[str, Any]]:
    """Deterministic relationship-preserving typed ID redaction for snapshots/diffs."""
    maps: dict[str, dict[str, str]] = {"trace": {}, "span": {}, "turn": {}, "branch": {}, "batch": {}}

    def _alias(kind: str, raw: str | None) -> str | None:
        if raw is None:
            return None
        table = maps[kind]
        if raw not in table:
            table[raw] = f"{{{{{kind}:{len(table) + 1}}}}}"
        return table[raw]

    out: list[dict[str, Any]] = []
    for ev in events:
        row = to_wire(ev)
        row.pop("ts_ms", None)
        row.pop("latency_ms", None)
        row["trace_id"] = _alias("trace", ev.trace_id)
        row["span_id"] = _alias("span", ev.span_id)
        row["parent_span_id"] = _alias("span", ev.parent_span_id)
        row["turn_id"] = _alias("turn", ev.turn_id)
        row["branch_id"] = _alias("branch", ev.branch_id)
        row["batch_id"] = _alias("batch", ev.batch_id)
        out.append(row)
    return out


def _sum_tape_usage(events: tuple[TapeEvent, ...]) -> TokenUsage:
    total = TokenUsage()
    for ev in events:
        if ev.usage is not None:
            total = total.add(ev.usage)
    return total


def diff_tapes(left: TapeReadResult, right: TapeReadResult) -> dict[str, Any]:
    """Compare two tape reads: manifest hash, counts, first divergence seq, and usage delta."""
    first_div: int | None = None
    for l_ev, r_ev in zip(left.events, right.events):
        if (l_ev.type, l_ev.payload) != (r_ev.type, r_ev.payload):
            first_div = l_ev.seq
            break
    if first_div is None and len(left.events) != len(right.events):
        first_div = min(len(left.events), len(right.events)) + 1

    l_usage = _sum_tape_usage(left.events)
    r_usage = _sum_tape_usage(right.events)
    l_types: dict[str, int] = {}
    for ev in left.events:
        l_types[ev.type] = l_types.get(ev.type, 0) + 1
    r_types: dict[str, int] = {}
    for ev in right.events:
        r_types[ev.type] = r_types.get(ev.type, 0) + 1

    return {
        "manifest_match": left.header.manifest_hash == right.header.manifest_hash,
        "left_manifest_hash": left.header.manifest_hash,
        "right_manifest_hash": right.header.manifest_hash,
        "left_status": left.status,
        "right_status": right.status,
        "left_events": len(left.events),
        "right_events": len(right.events),
        "first_divergence_seq": first_div,
        "event_type_delta": {
            k: r_types.get(k, 0) - l_types.get(k, 0)
            for k in sorted(set(l_types) | set(r_types))
        },
        "usage_delta": {
            "input_tokens": r_usage.input_tokens - l_usage.input_tokens,
            "output_tokens": r_usage.output_tokens - l_usage.output_tokens,
            "cache_read_tokens": r_usage.cache_read_tokens - l_usage.cache_read_tokens,
            "cache_write_tokens": r_usage.cache_write_tokens - l_usage.cache_write_tokens,
            "reasoning_tokens": r_usage.reasoning_tokens - l_usage.reasoning_tokens,
        },
    }
