"""Deterministic test doubles for LLM Driver: FakeModelClient, ReplayClient, and FakeLLMServer."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, AsyncGenerator, Literal

from xana.llm_driver.contracts import (
    ModelEvent,
    ModelRequest,
    ModelUsage,
    ProviderCapabilities,
    ProviderError,
)
from xana.llm_driver.interfaces import ModelClient
from xana.llm_driver.tape_recorder import compute_request_hash
from xana.protocol.primitives import BlobRef
from xana.protocol.tape import read_blob, read_tape

_DEFAULT_TOOL_NAME = "memory_search"
ReplayMode = Literal["request_hash", "sequence"]


@dataclass(frozen=True)
class ScriptedTurn:
    text_deltas: tuple[str | dict[str, Any], ...] = ()
    thinking_deltas: tuple[str, ...] = ()
    tool_call: tuple[str, dict[str, Any], str] | None = None
    tool_calls: tuple[tuple[str, dict[str, Any], str], ...] = ()
    usage: ModelUsage | None = None
    usage_input_tokens: int | None = None
    usage_output_tokens: int | None = None
    usage_cache_read_tokens: int | None = None
    usage_cache_write_tokens: int | None = None
    stop_reason: str = "stop"
    error: ProviderError | None = None

    def all_tool_calls(self) -> tuple[tuple[str, dict[str, Any], str], ...]:
        if self.tool_calls:
            return self.tool_calls
        if self.tool_call is not None:
            return (self.tool_call,)
        return ()

    def resolved_usage(self) -> ModelUsage | None:
        if self.usage is not None:
            return self.usage
        if any(
            v is not None
            for v in (
                self.usage_input_tokens,
                self.usage_output_tokens,
                self.usage_cache_read_tokens,
                self.usage_cache_write_tokens,
            )
        ):
            return ModelUsage(
                input_tokens=self.usage_input_tokens,
                output_tokens=self.usage_output_tokens,
                cache_read_tokens=self.usage_cache_read_tokens,
                cache_write_tokens=self.usage_cache_write_tokens,
            )
        return None


@dataclass(frozen=True)
class FaultStep:
    kind: Literal["http_error", "drop_mid_stream", "hang", "raw_sse"]
    status: int = 500
    body: str = "injected fault"
    headers: dict[str, str] = field(default_factory=dict)
    partial_text: str = ""
    delay_s: float = 0.0
    raw_chunks: tuple[str, ...] = ()


class FakeModelClient(ModelClient):
    """Scripted deterministic ModelClient (preserves legacy default round-0/round-1 behavior)."""

    def __init__(
        self,
        *script: ScriptedTurn,
        provider: str = "fake",
        capabilities: ProviderCapabilities | None = None,
    ) -> None:
        self._script = list(script)
        self._cursor = 0
        self._provider = provider
        self._capabilities = capabilities or ProviderCapabilities(
            provider=provider,
            supports_streaming=True,
            supports_tools=True,
            supports_parallel_tools=True,
        )
        self._recorded_requests: list[ModelRequest] = []

    @property
    def recorded_requests(self) -> list[ModelRequest]:
        return self._recorded_requests

    def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        self._recorded_requests.append(request)
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        if self._script:
            if self._cursor < len(self._script):
                turn = self._script[self._cursor]
                self._cursor += 1
                async for ev in self._emit_turn(turn):
                    yield ev
                return
            # Script exhausted: emit a bare done event so agent loops terminate cleanly.
            yield ModelEvent(kind="done", payload={"stop_reason": "stop"})
            return

        # Default behavior (backward-compatible with existing harness integration tests):
        has_tool_result = any(m.role == "tool" for m in request.messages)
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user" and not m.metadata.get("note_kind")),
            "",
        )
        if not has_tool_result:
            echo_text = " | ".join(m.content for m in request.messages if m.content)
            mid = max(1, len(echo_text) // 2)
            yield ModelEvent(kind="text_delta", payload={"text": echo_text[:mid]})
            if echo_text[mid:]:
                yield ModelEvent(kind="text_delta", payload={"text": echo_text[mid:]})
            yield ModelEvent(
                kind="tool_call",
                payload={"tool_name": _DEFAULT_TOOL_NAME, "input": {"query": last_user}, "call_id": "fake-call-1"},
            )
            yield ModelEvent(kind="usage", usage=ModelUsage(input_tokens=10, output_tokens=5))
            yield ModelEvent(kind="done", payload={"stop_reason": "tool_calls"})
        else:
            tool_out = next((m.content for m in reversed(request.messages) if m.role == "tool"), "")
            yield ModelEvent(
                kind="text_delta",
                payload={"text": f"Used {_DEFAULT_TOOL_NAME} ({tool_out[:80]}) [fake-reply ok]"},
            )
            yield ModelEvent(kind="usage", usage=ModelUsage(input_tokens=20, output_tokens=10))
            yield ModelEvent(kind="done", payload={"stop_reason": "stop"})

    async def _emit_turn(self, turn: ScriptedTurn) -> AsyncGenerator[ModelEvent, None]:
        for t in turn.thinking_deltas:
            yield ModelEvent(kind="text_delta", payload={"text": t, "channel": "thinking"})
        for d in turn.text_deltas:
            if isinstance(d, dict):
                yield ModelEvent(kind="text_delta", payload=dict(d))
            else:
                yield ModelEvent(kind="text_delta", payload={"text": d})
        for name, args, cid in turn.all_tool_calls():
            yield ModelEvent(kind="tool_call", payload={"tool_name": name, "input": args, "call_id": cid})
        if turn.error is not None:
            yield ModelEvent(kind="error", error=turn.error)
            return
        usage = turn.resolved_usage()
        if usage is not None:
            yield ModelEvent(kind="usage", usage=usage)
        yield ModelEvent(kind="done", payload={"stop_reason": turn.stop_reason})

    async def cancel(self, request_id: str) -> None:
        return None

    async def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        return self._capabilities


@dataclass(frozen=True)
class _RecordedEntry:
    request_hash: str
    status: str
    text: str
    thinking: str
    tool_calls: tuple[dict[str, Any], ...]
    stop_reason: str
    usage: ModelUsage | None
    error: ProviderError | None


class ReplayClient(ModelClient):
    """Replays recorded `llm.response` spans offline from a Stage 0 Tape bundle."""

    def __init__(self, entries: list[_RecordedEntry], *, mode: ReplayMode = "request_hash", provider: str = "replay") -> None:
        self._entries = list(entries)
        self._mode = mode
        self._provider = provider
        self._cursor = 0
        self._by_hash: dict[str, list[_RecordedEntry]] = {}
        for e in entries:
            self._by_hash.setdefault(e.request_hash, []).append(e)

    @classmethod
    def from_tape(cls, bundle_dir: Path, *, mode: ReplayMode = "request_hash") -> "ReplayClient":
        tape = read_tape(bundle_dir)
        entries: list[_RecordedEntry] = []
        for ev in tape.events:
            if ev.type != "llm.response":
                continue
            p = ev.payload
            content_raw = p.get("content_json")
            content: dict[str, Any] = {}
            if isinstance(content_raw, dict) and "__blob_ref__" in content_raw:
                ref_dict = content_raw["__blob_ref__"]
                ref = BlobRef(
                    blob_id=str(ref_dict.get("blob_id") or ref_dict["sha256"]),
                    sha256=str(ref_dict["sha256"]),
                    media_type=str(ref_dict.get("media_type", "application/json")),
                    original_bytes=int(ref_dict.get("original_bytes", 0)),
                    retained_bytes=int(ref_dict.get("retained_bytes", 0)),
                    capture=ref_dict.get("capture", "complete"),
                )
                blob_bytes = read_blob(Path(bundle_dir), ref)
                if blob_bytes is None:
                    raise ValueError(f"ReplayClient: blob {ref.sha256!r} is unavailable (capture={ref.capture})")
                content = json.loads(blob_bytes.decode("utf-8"))
            elif isinstance(content_raw, str) and content_raw:
                content = json.loads(content_raw)
            usage = None
            if ev.usage is not None:
                usage = ModelUsage(
                    input_tokens=ev.usage.input_tokens,
                    output_tokens=ev.usage.output_tokens,
                    cache_read_tokens=ev.usage.cache_read_tokens,
                    cache_write_tokens=ev.usage.cache_write_tokens,
                    reasoning_tokens=ev.usage.reasoning_tokens,
                    total_tokens=ev.usage.total_tokens,
                    cost_usd=ev.usage.cost_usd,
                )
            err = None
            if isinstance(p.get("error"), dict):
                ed = p["error"]
                details = ed.get("details") if isinstance(ed.get("details"), dict) else {}
                code = str(ed.get("code") or "")
                reason = str(details.get("reason") or (code.removeprefix("provider.") if code.startswith("provider.") else "unknown"))
                cat = str(ed.get("category") or "unknown")
                kind_map = {
                    "auth": "auth",
                    "rate_limit": "rate_limit",
                    "timeout": "timeout",
                    "network": "network",
                    "validation": "bad_request",
                    "cancelled": "cancelled",
                    "provider": "server_error",
                }
                err = ProviderError(
                    kind=kind_map.get(cat, "unknown"),  # type: ignore[arg-type]
                    message=str(ed.get("message") or "replayed error"),
                    retryable=bool(ed.get("retryable", False)),
                    raw_status=details.get("raw_status"),
                    request_id=details.get("request_id"),
                    reason=reason,  # type: ignore[arg-type]
                    retry_after_s=details.get("retry_after_s"),
                    should_compress=bool(details.get("should_compress", False)),
                    should_rotate_credential=bool(details.get("should_rotate_credential", False)),
                    should_fallback=bool(details.get("should_fallback", False)),
                )
            entries.append(_RecordedEntry(
                request_hash=str(p.get("request_hash") or ""),
                status=str(p.get("status") or "done"),
                text=str(content.get("text") or ""),
                thinking=str(content.get("thinking") or ""),
                tool_calls=tuple(content.get("tool_calls") or ()),
                stop_reason=str(p.get("stop_reason") or "stop"),
                usage=usage,
                error=err,
            ))
        return cls(entries, mode=mode)

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        entry: _RecordedEntry | None = None
        if self._mode == "request_hash":
            rh = compute_request_hash(request)
            bucket = self._by_hash.get(rh)
            if not bucket:
                raise KeyError(f"ReplayClient: no recorded response for request_hash={rh!r}")
            entry = bucket.pop(0)
        else:
            if self._cursor >= len(self._entries):
                raise IndexError("ReplayClient: exhausted recorded sequence")
            entry = self._entries[self._cursor]
            self._cursor += 1

        if entry.thinking:
            yield ModelEvent(kind="text_delta", payload={"text": entry.thinking, "channel": "thinking"})
        if entry.text:
            yield ModelEvent(kind="text_delta", payload={"text": entry.text})
        for tc in entry.tool_calls:
            yield ModelEvent(kind="tool_call", payload=dict(tc))
        if entry.error is not None:
            yield ModelEvent(kind="error", error=entry.error)
            return
        if entry.usage is not None:
            yield ModelEvent(kind="usage", usage=entry.usage)
        yield ModelEvent(kind="done", payload={"stop_reason": entry.stop_reason})

    async def cancel(self, request_id: str) -> None:
        return None

    async def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        return ProviderCapabilities(provider=self._provider, supports_streaming=True, supports_tools=True)


class FakeLLMServer:
    """Real local HTTP/SSE loopback server speaking OpenAI Chat Completions for wire tests."""

    def __init__(self, steps: list[ScriptedTurn | FaultStep]) -> None:
        self._steps = list(steps)
        self._idx = 0
        self._lock = threading.Lock()
        self.recorded_requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def start(self) -> "FakeLLMServer":
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    body = {}
                with outer._lock:
                    outer.recorded_requests.append(body)
                    if outer._idx < len(outer._steps):
                        step = outer._steps[outer._idx]
                        outer._idx += 1
                    else:
                        step = ScriptedTurn(text_deltas=("(default)",))

                if isinstance(step, FaultStep):
                    if step.kind == "http_error":
                        self.send_response(step.status)
                        self.send_header("Content-Type", "application/json")
                        for k, v in step.headers.items():
                            self.send_header(k, v)
                        payload = step.body.encode("utf-8")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    if step.kind == "hang":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        deadline = time.monotonic() + step.delay_s
                        while time.monotonic() < deadline:
                            time.sleep(0.05)
                        return
                    if step.kind == "drop_mid_stream":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        if step.partial_text:
                            chunk = f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': step.partial_text}}]})}\n\n"
                            self.wfile.write(chunk.encode("utf-8"))
                            self.wfile.flush()
                        return
                    if step.kind == "raw_sse":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        for raw_c in step.raw_chunks:
                            self.wfile.write(raw_c.encode("utf-8"))
                            self.wfile.flush()
                        return

                # ScriptedTurn -> standard OpenAI SSE
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("x-request-id", f"fake-req-{outer._idx}")
                self.end_headers()
                lines: list[str] = []
                for th in step.thinking_deltas:
                    lines.append(f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'reasoning_content': th}}]})}\n\n")
                for tx in step.text_deltas:
                    lines.append(f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': tx}}]})}\n\n")
                for idx, (name, args, cid) in enumerate(step.all_tool_calls()):
                    lines.append(
                        f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': idx, 'id': cid, 'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}}]})}\n\n"
                    )
                fr = "tool_calls" if step.all_tool_calls() else step.stop_reason
                lines.append(f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': fr}]})}\n\n")
                if step.usage is not None:
                    u_dict: dict[str, Any] = {}
                    cached = step.usage.cache_read_tokens or 0
                    if step.usage.input_tokens is not None:
                        u_dict["prompt_tokens"] = step.usage.input_tokens + cached
                    if step.usage.output_tokens is not None:
                        u_dict["completion_tokens"] = step.usage.output_tokens
                    if step.usage.cache_read_tokens is not None:
                        u_dict["cached_tokens"] = step.usage.cache_read_tokens
                    lines.append(f"data: {json.dumps({'choices': [], 'usage': u_dict})}\n\n")
                lines.append("data: [DONE]\n\n")
                for chunk in lines:
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "FakeLLMServer":
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.stop()
