"""Toggleable, tagged diagnostic logging for performance & functional anomaly analysis.

Stdlib-only (belongs to `xana.protocol`). Zero overhead when disabled.

Activation via env `XANA_DIAG` (or programmatic `configure_diag()`):
  - unset / "0" / "off" / "false" : disabled
  - "1" / "on" / "all" / "*"      : all tags at INFO level
  - "all:debug" / "*:debug"       : all tags at DEBUG level
  - "llm,policy:debug,tools"      : selective per-tag enablement & level

Log line format (single-line, greppable, secret-redacted):
  [xana:<tag>:<kind>] <event> k1=v1 k2=v2 ...
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

_ENV_VAR = "XANA_DIAG"
_SENSITIVE_KEY_PARTS = ("token", "secret", "password", "api_key", "apikey", "authorization", "cookie")

# None = read from env dynamically; dict[str, int] = programmatic override (tag -> level; "*" wildcard)
_OVERRIDE: dict[str, int] | None = None
_PARSED_ENV_RAW: str | None = None
_PARSED_ENV_MAP: dict[str, int] = {}


def _parse_spec(raw: str | bool | None) -> dict[str, int]:
    if raw is None or raw is False:
        return {}
    if raw is True:
        return {"*": logging.INFO}
    text = str(raw).strip()
    if not text or text.lower() in ("0", "off", "false", "no", "none"):
        return {}
    if text.lower() in ("1", "on", "true", "yes", "all", "*"):
        return {"*": logging.INFO}
    out: dict[str, int] = {}
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        if ":" in item:
            tag_raw, lvl_raw = item.split(":", 1)
            tag = tag_raw.strip().lower()
            lvl_name = lvl_raw.strip().lower()
            lvl = logging.DEBUG if lvl_name in ("debug", "d", "verbose") else logging.INFO
        else:
            tag = item.lower()
            lvl = logging.INFO
        if tag in ("all", "*"):
            out["*"] = min(out.get("*", logging.INFO), lvl)
        elif tag:
            out[tag] = min(out.get(tag, logging.INFO), lvl)
    return out


def configure_diag(spec: str | bool | None = None) -> None:
    """Programmatically toggle diagnostic logging (`None` reverts to `$XANA_DIAG`)."""
    global _OVERRIDE
    if spec is None:
        _OVERRIDE = None
    else:
        _OVERRIDE = _parse_spec(spec)


def _active_map() -> dict[str, int]:
    global _PARSED_ENV_RAW, _PARSED_ENV_MAP
    if _OVERRIDE is not None:
        return _OVERRIDE
    raw = os.environ.get(_ENV_VAR, "")
    if raw != _PARSED_ENV_RAW:
        _PARSED_ENV_RAW = raw
        _PARSED_ENV_MAP = _parse_spec(raw)
    return _PARSED_ENV_MAP


def diag_level(tag: str) -> int | None:
    """Return active logging level (INFO/DEBUG) for `tag`, or `None` if disabled."""
    active = _active_map()
    if not active:
        return None
    key = tag.lower()
    if key in active:
        return active[key]
    return active.get("*")


def diag_enabled(tag: str, *, detail: bool = False) -> bool:
    lvl = diag_level(tag)
    if lvl is None:
        return False
    return lvl <= logging.DEBUG if detail else True


def _sanitize_value(key: str, val: Any, *, max_len: int = 160) -> str:
    k_low = key.lower()
    if any(part in k_low for part in _SENSITIVE_KEY_PARTS):
        return "<redacted>"
    if isinstance(val, float):
        return f"{val:.2f}"
    text = str(val).replace("\n", "\\n").replace("\r", "")
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _format_fields(fields: dict[str, Any]) -> str:
    parts = [f"{k}={_sanitize_value(k, v)}" for k, v in fields.items() if v is not None]
    return (" " + " ".join(parts)) if parts else ""


class DiagLogger:
    """Lightweight tagged diagnostic logger for a subsystem (`llm`, `env`, `policy`, `tools`, ...)."""

    def __init__(self, tag: str, *, logger_name: str | None = None) -> None:
        self._tag = tag.lower()
        self._logger = logging.getLogger(logger_name or f"xana.diag.{self._tag}")

    @property
    def tag(self) -> str:
        return self._tag

    def enabled(self, *, detail: bool = False) -> bool:
        return diag_enabled(self._tag, detail=detail)

    def op(self, event: str, **fields: Any) -> None:
        if not self.enabled():
            return
        self._logger.info("[xana:%s:op] %s%s", self._tag, event, _format_fields(fields))

    def perf(
        self,
        event: str,
        *,
        latency_ms: float,
        slow_ms: float | None = None,
        **fields: Any,
    ) -> None:
        if not self.enabled():
            return
        is_slow = slow_ms is not None and latency_ms >= slow_ms
        kind = "perf:slow" if is_slow else "perf"
        msg = f"[xana:{self._tag}:{kind}] {event} latency_ms={latency_ms:.2f}{_format_fields(fields)}"
        if is_slow:
            self._logger.warning("%s", msg)
        else:
            self._logger.info("%s", msg)

    def anomaly(self, event: str, **fields: Any) -> None:
        if not self.enabled():
            return
        self._logger.warning("[xana:%s:anomaly] %s%s", self._tag, event, _format_fields(fields))

    def detail(self, event: str, **fields: Any) -> None:
        if not self.enabled(detail=True):
            return
        self._logger.debug("[xana:%s:detail] %s%s", self._tag, event, _format_fields(fields))

    @contextmanager
    def span(self, event: str, *, slow_ms: float | None = None, **fields: Any) -> Iterator[dict[str, Any]]:
        """Measure elapsed wall time for `event`; emits `perf` (or `anomaly` on exception) if enabled."""
        extra: dict[str, Any] = dict(fields)
        if not self.enabled():
            yield extra
            return
        t0 = time.monotonic()
        try:
            yield extra
        except BaseException as exc:
            elapsed = (time.monotonic() - t0) * 1000.0
            self.anomaly(f"{event}.error", latency_ms=elapsed, exc=type(exc).__name__, msg=str(exc), **extra)
            raise
        else:
            elapsed = (time.monotonic() - t0) * 1000.0
            self.perf(event, latency_ms=elapsed, slow_ms=slow_ms, **extra)
