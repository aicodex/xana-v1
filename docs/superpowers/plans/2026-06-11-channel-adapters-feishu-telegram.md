# Channel Adapters: Feishu + Telegram Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Feishu and Telegram channel adapters to xana, each conforming to the existing `Adapter` interface (`src/xana/agent_adapter/interfaces.py`). Both adapters run inside a long-lived process started by a new `xana serve` CLI command, share an `external_thread_id → session_id` binding map, and stream `AgentEvent`s back to their respective platforms with live editing.

**Architecture:**
- The existing `Adapter` ABC is channel-agnostic — no contract changes needed. Both adapters implement `start`/`stop`/`receive`/`render`/`bind_session`/`health`.
- A new `xana/cli/serve.py` boot path starts adapters in parallel, registers them on the gateway, and routes inbound messages through `gateway.submit()`. The CLI takes `--feishu` and `--telegram` flags, each gated by env-var presence.
- Each adapter implements its own:
  - Inbound: aiohttp webhook server (Feishu) and long-poll loop (Telegram).
  - Outbound: HTTP PATCH to edit a streaming "draft" message on each `draft` event; finalize sends a sealed card/message with footer.
  - Throttle: ≥ 250ms between PATCH calls (mirrors yuxu_backup's `DraftHandle` throttle) to stay under platform rate limits.
- Session mapping: per-adapter dict `external_thread_id → session_id`, persisted in a sidecar JSON under `~/.xana/channels/{adapter_id}.bindings.json`. First message from a new chat allocates a session_id; subsequent messages reuse it.
- HMAC verification for Feishu webhooks; `X-Telegram-Bot-Api-Secret-Token` for Telegram webhooks (optional, default long-poll).

**Tech Stack:** Python 3.10+, `aiohttp` (new dep, both adapters), `httpx` (already present). Optional: `lark-oapi` (Feishu SDK) — but we'll use plain HTTP to avoid the heavy dep.

**File structure:**
- Modify: `pyproject.toml` — add `aiohttp>=3.9`
- Create: `src/xana/agent_adapter/channels/__init__.py`
- Create: `src/xana/agent_adapter/channels/_bindings.py` — sidecar JSON binding store
- Create: `src/xana/agent_adapter/channels/_throttle.py` — async throttle helper
- Create: `src/xana/agent_adapter/channels/feishu.py` — `FeishuAdapter`
- Create: `src/xana/agent_adapter/channels/feishu_webhook.py` — aiohttp app
- Create: `src/xana/agent_adapter/channels/feishu_events.py` — parsing + HMAC
- Create: `src/xana/agent_adapter/channels/telegram.py` — `TelegramAdapter`
- Create: `src/xana/agent_adapter/channels/telegram_client.py` — Bot API HTTP wrapper
- Create: `src/xana/cli/serve.py` — `xana serve` command
- Modify: `src/xana/cli/xana.py` — register `serve` subcommand
- Test: `tests/test_channel_bindings.py`
- Test: `tests/test_feishu_events.py`
- Test: `tests/test_feishu_adapter.py`
- Test: `tests/test_telegram_adapter.py`
- Test: `tests/test_serve_command.py`

---

### Task 1: Add aiohttp dependency

**Files:**
- Modify: `pyproject.toml`

- [x] **Step 1: Edit pyproject.toml**

```toml
dependencies = [
    "pydantic>=2",
    "httpx>=0.28",
    "pyyaml>=6",
    "prompt_toolkit>=3.0",
    "aiohttp>=3.9",
]
```

- [x] **Step 2: Install editable**

Run: `pip install -e .`
Expected: aiohttp installed.

- [x] **Step 3: Smoke import**

Run: `python -c "import aiohttp; print(aiohttp.__version__)"`
Expected: `3.9.x` or higher.

- [x] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore(deps): add aiohttp for channel adapters"
```

---

### Task 2: BindingStore for external_thread_id → session_id

**Files:**
- Create: `src/xana/agent_adapter/channels/__init__.py`
- Create: `src/xana/agent_adapter/channels/_bindings.py`
- Test: `tests/test_channel_bindings.py`

- [x] **Step 1: Write failing tests**

Create `tests/test_channel_bindings.py`:

```python
from __future__ import annotations

from pathlib import Path

from xana.agent_adapter.channels._bindings import BindingStore


def test_get_unknown_returns_none(tmp_path: Path) -> None:
    store = BindingStore(path=tmp_path / "b.json")
    assert store.get("chat-1") is None


def test_put_then_get_roundtrip(tmp_path: Path) -> None:
    store = BindingStore(path=tmp_path / "b.json")
    store.put("chat-1", "session-a")
    assert store.get("chat-1") == "session-a"


def test_put_persists_to_disk(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    store = BindingStore(path=p)
    store.put("chat-1", "session-a")
    store2 = BindingStore(path=p)
    assert store2.get("chat-1") == "session-a"


def test_put_overwrites_existing(tmp_path: Path) -> None:
    store = BindingStore(path=tmp_path / "b.json")
    store.put("chat-1", "session-a")
    store.put("chat-1", "session-b")
    assert store.get("chat-1") == "session-b"


def test_allocate_returns_existing_or_creates(tmp_path: Path) -> None:
    store = BindingStore(path=tmp_path / "b.json")
    sid = store.allocate("chat-1", prefix="cli")
    assert sid.startswith("cli-")
    assert store.allocate("chat-1", prefix="cli") == sid
```

- [x] **Step 2: Run to verify fail**

Run: `pytest tests/test_channel_bindings.py -v`
Expected: fail — module missing.

- [x] **Step 3: Implement BindingStore**

Create `src/xana/agent_adapter/channels/__init__.py`:

```python
"""Channel adapters: Feishu, Telegram. Run inside `xana serve`."""
```

Create `src/xana/agent_adapter/channels/_bindings.py`:

```python
"""BindingStore: sidecar JSON mapping external_thread_id → session_id.

Each channel adapter owns one. Stores are loaded eagerly on construction
and saved atomically on every put — channel processes are long-lived but
race-tolerant.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path


class BindingStore:
    def __init__(self, *, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, str] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def get(self, external_thread_id: str) -> str | None:
        return self._data.get(external_thread_id)

    def put(self, external_thread_id: str, session_id: str) -> None:
        self._data[external_thread_id] = session_id
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def allocate(self, external_thread_id: str, *, prefix: str) -> str:
        existing = self.get(external_thread_id)
        if existing is not None:
            return existing
        sid = f"{prefix}-{secrets.token_hex(6)}"
        self.put(external_thread_id, sid)
        return sid
```

- [x] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_channel_bindings.py -v`
Expected: 5 passed

- [x] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/channels/__init__.py src/xana/agent_adapter/channels/_bindings.py tests/test_channel_bindings.py
git commit -m "feat(channels): BindingStore — sidecar JSON for thread→session mapping"
```

---

### Task 3: Throttle helper

**Files:**
- Create: `src/xana/agent_adapter/channels/_throttle.py`
- Test: `tests/test_throttle.py`

- [x] **Step 1: Write failing tests**

Create `tests/test_throttle.py`:

```python
from __future__ import annotations

import asyncio
import time

import pytest

from xana.agent_adapter.channels._throttle import Throttle


@pytest.mark.asyncio
async def test_first_call_does_not_wait() -> None:
    t = Throttle(min_interval_s=0.25)
    start = time.monotonic()
    await t.wait()
    assert time.monotonic() - start < 0.05


@pytest.mark.asyncio
async def test_second_call_waits_for_interval() -> None:
    t = Throttle(min_interval_s=0.1)
    await t.wait()
    start = time.monotonic()
    await t.wait()
    assert time.monotonic() - start >= 0.09


@pytest.mark.asyncio
async def test_two_throttles_are_independent() -> None:
    a = Throttle(min_interval_s=0.1)
    b = Throttle(min_interval_s=0.1)
    await a.wait()
    start = time.monotonic()
    await b.wait()  # b never marked, should be immediate
    assert time.monotonic() - start < 0.05
```

- [x] **Step 2: Run to verify fail**

Run: `pytest tests/test_throttle.py -v`
Expected: fail.

- [x] **Step 3: Implement Throttle**

Create `src/xana/agent_adapter/channels/_throttle.py`:

```python
"""Throttle: enforce min_interval_s between async actions.

Used by channel adapters to rate-limit live message-edit calls. Each
adapter chat owns its own Throttle instance — global is the wrong scope.
"""
from __future__ import annotations

import asyncio
import time


class Throttle:
    def __init__(self, *, min_interval_s: float) -> None:
        self.min_interval_s = min_interval_s
        self._last_ts: float | None = None

    async def wait(self) -> None:
        now = time.monotonic()
        if self._last_ts is not None:
            elapsed = now - self._last_ts
            remaining = self.min_interval_s - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_ts = time.monotonic()
```

- [x] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_throttle.py -v`
Expected: 3 passed

- [x] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/channels/_throttle.py tests/test_throttle.py
git commit -m "feat(channels): Throttle helper for edit-API rate limiting"
```

---

### Task 4: Feishu event parser + HMAC

**Files:**
- Create: `src/xana/agent_adapter/channels/feishu_events.py`
- Test: `tests/test_feishu_events.py`

- [x] **Step 1: Write failing tests**

Create `tests/test_feishu_events.py`:

```python
from __future__ import annotations

import hashlib
import json

import pytest

from xana.agent_adapter.channels.feishu_events import (
    FeishuVerifyError, parse_message_event, verify_signature,
)


def test_verify_signature_accepts_correct_digest() -> None:
    body = b'{"hello":"world"}'
    ts = "1234"
    nonce = "abcd"
    key = "secret-key"
    digest = hashlib.sha256((ts + nonce + key).encode() + body).hexdigest()
    verify_signature(timestamp=ts, nonce=nonce, body=body, encrypt_key=key, signature=digest)


def test_verify_signature_rejects_bad_digest() -> None:
    with pytest.raises(FeishuVerifyError):
        verify_signature(timestamp="1", nonce="n", body=b"x",
                         encrypt_key="k", signature="nope")


def test_parse_p2p_text_message() -> None:
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {
                "chat_id": "oc_test", "chat_type": "p2p",
                "message_id": "om_x", "message_type": "text",
                "content": json.dumps({"text": "hello"}),
            },
        },
    }
    parsed = parse_message_event(event)
    assert parsed is not None
    assert parsed.chat_id == "oc_test"
    assert parsed.chat_type == "p2p"
    assert parsed.text == "hello"
    assert parsed.sender_open_id == "ou_abc"


def test_parse_ignores_non_text_messages() -> None:
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {
                "chat_id": "oc_test", "chat_type": "p2p",
                "message_id": "om_x", "message_type": "image",
                "content": json.dumps({"image_key": "k"}),
            },
        },
    }
    parsed = parse_message_event(event)
    assert parsed is None  # MVP: text only
```

- [x] **Step 2: Run to verify fail**

Run: `pytest tests/test_feishu_events.py -v`
Expected: fail — module missing.

- [x] **Step 3: Implement feishu_events.py**

Create `src/xana/agent_adapter/channels/feishu_events.py`:

```python
"""Feishu event parsing + HMAC signature verification.

Targets the "v2" event format (header + event envelopes). For MVP we only
recognize `im.message.receive_v1` text messages. Anything else returns None
from parse_message_event() and the adapter ignores it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any


class FeishuVerifyError(Exception):
    pass


def verify_signature(*, timestamp: str, nonce: str, body: bytes,
                     encrypt_key: str, signature: str) -> None:
    """Raise FeishuVerifyError if `signature` doesn't match the canonical digest."""
    if not (timestamp and nonce and encrypt_key and signature):
        raise FeishuVerifyError("missing signature components")
    to_sign = (timestamp + nonce + encrypt_key).encode("utf-8") + body
    digest = hashlib.sha256(to_sign).hexdigest()
    if not hmac.compare_digest(digest, signature):
        raise FeishuVerifyError("signature mismatch")


@dataclass(frozen=True)
class ParsedMessageEvent:
    chat_id: str
    chat_type: str  # "p2p" | "group"
    message_id: str
    message_type: str
    text: str
    sender_open_id: str | None


def parse_message_event(event: dict[str, Any]) -> ParsedMessageEvent | None:
    header = event.get("header") or {}
    if header.get("event_type") != "im.message.receive_v1":
        return None
    body = event.get("event") or {}
    msg = body.get("message") or {}
    if msg.get("message_type") != "text":
        return None
    content_raw = msg.get("content") or "{}"
    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError:
        return None
    text = str(content.get("text", "")).strip()
    if not text:
        return None
    sender = body.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    return ParsedMessageEvent(
        chat_id=str(msg.get("chat_id") or ""),
        chat_type=str(msg.get("chat_type") or "p2p"),
        message_id=str(msg.get("message_id") or ""),
        message_type=str(msg.get("message_type") or "text"),
        text=text,
        sender_open_id=sender_id.get("open_id"),
    )
```

- [x] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_feishu_events.py -v`
Expected: 4 passed

- [x] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/channels/feishu_events.py tests/test_feishu_events.py
git commit -m "feat(channels): Feishu event parser + HMAC verifier"
```

---

### Task 5: FeishuAdapter — inbound + render

**Files:**
- Create: `src/xana/agent_adapter/channels/feishu.py`
- Create: `src/xana/agent_adapter/channels/feishu_webhook.py`
- Test: `tests/test_feishu_adapter.py`

- [x] **Step 1: Write failing test for inbound queue**

Create `tests/test_feishu_adapter.py`:

```python
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from xana.agent_adapter.channels.feishu import FeishuAdapter, FeishuConfig
from xana.agent_adapter.channels._bindings import BindingStore
from xana.agents.contracts import AgentEvent


@pytest.mark.asyncio
async def test_inbound_text_event_lands_in_receive(tmp_path: Path) -> None:
    cfg = FeishuConfig(app_id="x", app_secret="y", verification_token="vt", encrypt_key=None)
    adapter = FeishuAdapter(config=cfg, bindings=BindingStore(path=tmp_path / "b.json"))
    await adapter.start()
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_alice"}},
            "message": {
                "chat_id": "oc_test", "chat_type": "p2p",
                "message_id": "om_x", "message_type": "text",
                "content": json.dumps({"text": "hi"}),
            },
        },
    }
    await adapter._on_event(event)
    msgs = []
    async for m in adapter.receive():
        msgs.append(m)
        break
    assert msgs[0].text == "hi"
    assert msgs[0].user_id == "ou_alice"
    await adapter.stop()


@pytest.mark.asyncio
async def test_render_draft_event_buffers_text_for_edit(tmp_path: Path) -> None:
    """Draft chunks accumulate in a per-session buffer; render returns the cumulative text."""
    cfg = FeishuConfig(app_id="x", app_secret="y", verification_token="vt", encrypt_key=None)
    adapter = FeishuAdapter(config=cfg, bindings=BindingStore(path=tmp_path / "b.json"),
                            api_sender=_SpyApi())
    rendered = await adapter.render(AgentEvent(kind="draft", session_id="s1", payload={"text": "hi"}))
    assert "hi" in rendered.text
    rendered2 = await adapter.render(AgentEvent(kind="draft", session_id="s1", payload={"text": " there"}))
    assert "hi there" in rendered2.text


@pytest.mark.asyncio
async def test_final_event_flushes_footer_card(tmp_path: Path) -> None:
    api = _SpyApi()
    cfg = FeishuConfig(app_id="x", app_secret="y", verification_token="vt", encrypt_key=None)
    adapter = FeishuAdapter(config=cfg, bindings=BindingStore(path=tmp_path / "b.json"), api_sender=api)
    await adapter.bind_session_for_chat("oc_test", "s1")
    await adapter.render(AgentEvent(kind="draft", session_id="s1", payload={"text": "answer"}))
    await adapter.render(AgentEvent(kind="final", session_id="s1", payload={"text": "answer"}))
    assert any("hr" in str(card) or "divider" in str(card).lower() or "footer" in str(card).lower()
               for card in api.cards_sent)


class _SpyApi:
    def __init__(self) -> None:
        self.cards_sent: list = []
        self.last_message_id: str | None = None

    async def send_card(self, *, receive_id: str, card: dict) -> str:
        self.cards_sent.append(card)
        self.last_message_id = f"mid-{len(self.cards_sent)}"
        return self.last_message_id

    async def patch_card(self, *, message_id: str, card: dict) -> None:
        self.cards_sent.append(card)
```

- [x] **Step 2: Run to verify fail**

Run: `pytest tests/test_feishu_adapter.py -v`
Expected: fail — module missing.

- [x] **Step 3: Implement FeishuAdapter**

Create `src/xana/agent_adapter/channels/feishu.py`:

```python
"""FeishuAdapter: maps Feishu webhook events into xana's Adapter interface.

Inbound: webhook server (feishu_webhook.py) calls _on_event; the adapter
parses the event and pushes an InboundMessage onto an asyncio.Queue.
Outbound: each session_id owns a small buffer (cumulative text, last
message_id). draft events PATCH the live card; final event appends the
footer divider + meta line and seals.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

from xana.agent_adapter.contracts import (
    AdapterHealth, InboundMessage, RenderedMessage, SessionBinding,
)
from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels._throttle import Throttle
from xana.agent_adapter.channels.feishu_events import parse_message_event
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str | None
    api_base: str = "https://open.feishu.cn"
    bot_open_id: str | None = None


class FeishuApiSender(Protocol):
    async def send_card(self, *, receive_id: str, card: dict) -> str: ...
    async def patch_card(self, *, message_id: str, card: dict) -> None: ...


@dataclass
class _SessionBuffer:
    cumulative_text: str = ""
    last_message_id: str | None = None
    throttle: Throttle = field(default_factory=lambda: Throttle(min_interval_s=0.25))
    chat_id: str | None = None


class FeishuAdapter(Adapter):
    adapter_id = "feishu"

    def __init__(
        self, *,
        config: FeishuConfig,
        bindings: BindingStore,
        api_sender: FeishuApiSender | None = None,
    ) -> None:
        self.config = config
        self.bindings = bindings
        self.api_sender = api_sender  # injected in tests; real default constructed in start()
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._buffers: dict[str, _SessionBuffer] = {}
        self._status = "stopped"

    async def start(self) -> None:
        if self.api_sender is None:
            # Real sender — lazy import to avoid pulling httpx at import time.
            from xana.agent_adapter.channels.feishu_webhook import build_real_sender
            self.api_sender = build_real_sender(self.config)
        self._status = "healthy"

    async def stop(self) -> None:
        self._status = "stopped"

    async def bind_session_for_chat(self, chat_id: str, session_id: str) -> None:
        buf = self._buffers.setdefault(session_id, _SessionBuffer())
        buf.chat_id = chat_id

    async def _on_event(self, event: dict) -> None:
        parsed = parse_message_event(event)
        if parsed is None:
            return
        sid = self.bindings.allocate(parsed.chat_id, prefix="feishu")
        await self.bind_session_for_chat(parsed.chat_id, sid)
        await self._inbound.put(InboundMessage(
            adapter_id=self.adapter_id,
            session_id=sid,
            text=parsed.text,
            user_id=parsed.sender_open_id,
            metadata={"chat_id": parsed.chat_id, "message_id": parsed.message_id},
        ))

    def receive(self) -> AsyncIterator[InboundMessage]:
        return self._receive_gen()

    async def _receive_gen(self):
        while True:
            yield await self._inbound.get()

    async def render(self, event: AgentEvent) -> RenderedMessage:
        text = str(event.payload.get("text", ""))
        buf = self._buffers.setdefault(event.session_id, _SessionBuffer())
        if event.kind == "draft":
            buf.cumulative_text += text
            await self._patch_or_send(buf, finalize=False)
        elif event.kind == "final":
            await self._patch_or_send(buf, finalize=True, footer_meta=self._footer_meta(event))
        elif event.kind == "error":
            buf.cumulative_text += f"\n[error] {text}"
            await self._patch_or_send(buf, finalize=True)
        return RenderedMessage(adapter_id=self.adapter_id, session_id=event.session_id,
                               text=buf.cumulative_text, metadata={"event_kind": event.kind})

    def _footer_meta(self, event: AgentEvent) -> list[tuple[str, str]]:
        h = getattr(event, "harness_event", None)
        usage = getattr(h, "usage", None) if h else None
        meta: list[tuple[str, str]] = []
        if usage:
            if usage.model:
                meta.append(("model", str(usage.model)))
            if usage.exact_input_tokens is not None or usage.exact_output_tokens is not None:
                meta.append(("tokens", f"{usage.exact_input_tokens or 0} in / {usage.exact_output_tokens or 0} out"))
        return meta

    async def _patch_or_send(self, buf: _SessionBuffer, *, finalize: bool,
                              footer_meta: list[tuple[str, str]] | None = None) -> None:
        if buf.chat_id is None or self.api_sender is None:
            return
        await buf.throttle.wait()
        card = self._build_card(buf.cumulative_text, footer_meta=footer_meta, finalize=finalize)
        if buf.last_message_id is None:
            mid = await self.api_sender.send_card(receive_id=buf.chat_id, card=card)
            buf.last_message_id = mid
        else:
            await self.api_sender.patch_card(message_id=buf.last_message_id, card=card)

    def _build_card(self, text: str, *, footer_meta: list[tuple[str, str]] | None,
                     finalize: bool) -> dict:
        elements: list = []
        if text:
            elements.append({"tag": "markdown", "content": text})
        if finalize and footer_meta:
            elements.append({"tag": "hr"})
            meta_line = " | ".join(f"{k}: {v}" for k, v in footer_meta)
            elements.append({"tag": "markdown", "content": f"<font color='grey'>{meta_line}</font>"})
        return {"schema": "2.0", "config": {"width_mode": "fill"}, "body": {"elements": elements}}

    async def bind_session(self, binding: SessionBinding) -> None:
        # binding.external_thread_id is the chat_id
        await self.bind_session_for_chat(binding.external_thread_id, binding.session_id)

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, status=self._status)  # type: ignore[arg-type]
```

- [x] **Step 4: Create webhook + real API sender stub**

Create `src/xana/agent_adapter/channels/feishu_webhook.py`:

```python
"""aiohttp webhook + httpx-based Feishu sender."""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

import httpx
from aiohttp import web

from xana.agent_adapter.channels.feishu import FeishuApiSender, FeishuConfig
from xana.agent_adapter.channels.feishu_events import FeishuVerifyError, verify_signature


def build_webhook_app(
    *,
    config: FeishuConfig,
    on_event: Callable[[dict], Awaitable[None]],
) -> web.Application:
    async def handler(request: web.Request) -> web.Response:
        body = await request.read()
        # URL verification challenge (Feishu sends this once on registration).
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return web.json_response({"error": "bad json"}, status=400)
        if payload.get("type") == "url_verification":
            if payload.get("token") != config.verification_token:
                return web.json_response({"error": "token mismatch"}, status=403)
            return web.json_response({"challenge": payload.get("challenge")})
        # Real event — optionally verify HMAC.
        if config.encrypt_key:
            sig = request.headers.get("X-Lark-Signature")
            ts = request.headers.get("X-Lark-Request-Timestamp", "")
            nonce = request.headers.get("X-Lark-Request-Nonce", "")
            try:
                verify_signature(timestamp=ts, nonce=nonce, body=body,
                                 encrypt_key=config.encrypt_key, signature=sig or "")
            except FeishuVerifyError as e:
                return web.json_response({"error": str(e)}, status=403)
        await on_event(payload)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/feishu/webhook", handler)
    return app


class _HttpxFeishuSender(FeishuApiSender):
    def __init__(self, config: FeishuConfig) -> None:
        self.config = config
        self._client = httpx.AsyncClient(base_url=config.api_base, timeout=10.0)
        self._token: str | None = None
        self._token_lock = asyncio.Lock()

    async def _ensure_token(self) -> str:
        async with self._token_lock:
            if self._token:
                return self._token
            resp = await self._client.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
            )
            self._token = resp.json()["tenant_access_token"]
            return self._token

    async def send_card(self, *, receive_id: str, card: dict) -> str:
        token = await self._ensure_token()
        resp = await self._client.post(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": receive_id, "msg_type": "interactive",
                  "content": json.dumps(card, ensure_ascii=False)},
        )
        data = resp.json()
        return data.get("data", {}).get("message_id", "")

    async def patch_card(self, *, message_id: str, card: dict) -> None:
        token = await self._ensure_token()
        await self._client.patch(
            f"/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": json.dumps(card, ensure_ascii=False)},
        )


def build_real_sender(config: FeishuConfig) -> FeishuApiSender:
    return _HttpxFeishuSender(config)
```

- [x] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_feishu_adapter.py -v`
Expected: 3 passed

- [x] **Step 6: Commit**

```bash
git add src/xana/agent_adapter/channels/feishu.py src/xana/agent_adapter/channels/feishu_webhook.py tests/test_feishu_adapter.py
git commit -m "feat(channels): FeishuAdapter + webhook + httpx sender"
```

---

### Task 6: TelegramAdapter — long-poll + send/edit

**Files:**
- Create: `src/xana/agent_adapter/channels/telegram.py`
- Create: `src/xana/agent_adapter/channels/telegram_client.py`
- Test: `tests/test_telegram_adapter.py`

- [x] **Step 1: Write failing tests**

Create `tests/test_telegram_adapter.py`:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels.telegram import TelegramAdapter, TelegramConfig
from xana.agents.contracts import AgentEvent


@pytest.mark.asyncio
async def test_dispatch_text_update_enqueues_inbound(tmp_path: Path) -> None:
    cfg = TelegramConfig(bot_token="t")
    adapter = TelegramAdapter(config=cfg, bindings=BindingStore(path=tmp_path / "b.json"),
                              api_client=_SpyClient())
    update = {
        "update_id": 1,
        "message": {"message_id": 99, "from": {"id": 42}, "chat": {"id": 9999, "type": "private"},
                    "text": "hello"},
    }
    await adapter._dispatch_update(update)
    msgs = []
    async for m in adapter.receive():
        msgs.append(m)
        break
    assert msgs[0].text == "hello"
    assert msgs[0].user_id == "42"


@pytest.mark.asyncio
async def test_render_draft_streams_via_edit_message_text(tmp_path: Path) -> None:
    client = _SpyClient()
    adapter = TelegramAdapter(config=TelegramConfig(bot_token="t"),
                              bindings=BindingStore(path=tmp_path / "b.json"),
                              api_client=client)
    await adapter.bind_session_for_chat("9999", "s1")
    await adapter.render(AgentEvent(kind="draft", session_id="s1", payload={"text": "hi"}))
    assert client.sent or client.edited
    await adapter.render(AgentEvent(kind="draft", session_id="s1", payload={"text": " there"}))
    assert client.edited  # second draft must be an edit, not a new send


@pytest.mark.asyncio
async def test_final_appends_footer_to_message(tmp_path: Path) -> None:
    client = _SpyClient()
    adapter = TelegramAdapter(config=TelegramConfig(bot_token="t"),
                              bindings=BindingStore(path=tmp_path / "b.json"),
                              api_client=client)
    await adapter.bind_session_for_chat("9999", "s1")
    await adapter.render(AgentEvent(kind="draft", session_id="s1", payload={"text": "answer"}))
    await adapter.render(AgentEvent(kind="final", session_id="s1", payload={"text": "answer"}))
    final_call = client.edited[-1]
    assert "—" in final_call["text"] or "_" in final_call["text"]


class _SpyClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []

    async def send_message(self, *, chat_id: str, text: str, parse_mode: str | None = None) -> int:
        self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
        return len(self.sent)  # fake message_id

    async def edit_message_text(self, *, chat_id: str, message_id: int, text: str,
                                 parse_mode: str | None = None) -> None:
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        return []
```

- [x] **Step 2: Run to verify fail**

Run: `pytest tests/test_telegram_adapter.py -v`
Expected: fail — module missing.

- [x] **Step 3: Implement TelegramAdapter**

Create `src/xana/agent_adapter/channels/telegram.py`:

```python
"""TelegramAdapter: Telegram Bot API channel via long-poll.

Inbound: a background task pulls updates via getUpdates (timeout 25s)
and feeds them to _dispatch_update, which queues InboundMessages.
Outbound: per-session buffer; first draft → send_message; subsequent
drafts → edit_message_text; final → edit_message_text with footer line.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels._throttle import Throttle
from xana.agent_adapter.contracts import (
    AdapterHealth, InboundMessage, RenderedMessage, SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    api_base: str = "https://api.telegram.org"
    poll_timeout_s: int = 25
    allowed_user_ids: frozenset[int] = frozenset()


class TelegramApiClient(Protocol):
    async def send_message(self, *, chat_id: str, text: str, parse_mode: str | None = None) -> int: ...
    async def edit_message_text(self, *, chat_id: str, message_id: int, text: str,
                                 parse_mode: str | None = None) -> None: ...
    async def get_updates(self, *, offset: int | None, timeout: int) -> list[dict]: ...


@dataclass
class _SessionBuffer:
    cumulative_text: str = ""
    message_id: int | None = None
    throttle: Throttle = field(default_factory=lambda: Throttle(min_interval_s=0.25))
    chat_id: str | None = None


class TelegramAdapter(Adapter):
    adapter_id = "telegram"

    def __init__(
        self, *,
        config: TelegramConfig,
        bindings: BindingStore,
        api_client: TelegramApiClient | None = None,
    ) -> None:
        self.config = config
        self.bindings = bindings
        self.api_client = api_client
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._buffers: dict[str, _SessionBuffer] = {}
        self._poll_task: asyncio.Task | None = None
        self._offset: int | None = None
        self._status = "stopped"

    async def start(self) -> None:
        if self.api_client is None:
            from xana.agent_adapter.channels.telegram_client import HttpxTelegramClient
            self.api_client = HttpxTelegramClient(self.config)
        self._status = "healthy"
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._status = "stopped"
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def bind_session_for_chat(self, chat_id: str, session_id: str) -> None:
        buf = self._buffers.setdefault(session_id, _SessionBuffer())
        buf.chat_id = chat_id

    async def _poll_loop(self) -> None:
        assert self.api_client is not None
        while True:
            try:
                updates = await self.api_client.get_updates(
                    offset=self._offset, timeout=self.config.poll_timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)
                continue
            for u in updates:
                uid = int(u.get("update_id", 0))
                if self._offset is None or uid >= self._offset:
                    self._offset = uid + 1
                await self._dispatch_update(u)

    async def _dispatch_update(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg or "text" not in msg:
            return
        from_user = msg.get("from") or {}
        if self.config.allowed_user_ids and from_user.get("id") not in self.config.allowed_user_ids:
            return
        chat_id = str((msg.get("chat") or {}).get("id"))
        sid = self.bindings.allocate(chat_id, prefix="tg")
        await self.bind_session_for_chat(chat_id, sid)
        await self._inbound.put(InboundMessage(
            adapter_id=self.adapter_id,
            session_id=sid,
            text=msg["text"],
            user_id=str(from_user.get("id")) if from_user.get("id") is not None else None,
            metadata={"chat_id": chat_id, "message_id": msg.get("message_id")},
        ))

    def receive(self) -> AsyncIterator[InboundMessage]:
        return self._receive_gen()

    async def _receive_gen(self):
        while True:
            yield await self._inbound.get()

    async def render(self, event: AgentEvent) -> RenderedMessage:
        text = str(event.payload.get("text", ""))
        buf = self._buffers.setdefault(event.session_id, _SessionBuffer())
        if event.kind == "draft":
            buf.cumulative_text += text
            await self._send_or_edit(buf, finalize=False)
        elif event.kind == "final":
            footer = self._build_footer(event)
            if footer:
                buf.cumulative_text = buf.cumulative_text.rstrip() + "\n\n――――――――――――\n_" + footer + "_"
            await self._send_or_edit(buf, finalize=True)
        elif event.kind == "error":
            buf.cumulative_text += f"\n[error] {text}"
            await self._send_or_edit(buf, finalize=True)
        return RenderedMessage(adapter_id=self.adapter_id, session_id=event.session_id,
                               text=buf.cumulative_text, metadata={"event_kind": event.kind})

    def _build_footer(self, event: AgentEvent) -> str:
        h = getattr(event, "harness_event", None)
        usage = getattr(h, "usage", None) if h else None
        if not usage:
            return ""
        parts: list[str] = []
        if usage.model:
            parts.append(f"model: {usage.model}")
        if usage.exact_input_tokens is not None or usage.exact_output_tokens is not None:
            parts.append(f"tokens: {usage.exact_input_tokens or 0} in / {usage.exact_output_tokens or 0} out")
        return " | ".join(parts)

    async def _send_or_edit(self, buf: _SessionBuffer, *, finalize: bool) -> None:
        if buf.chat_id is None or self.api_client is None:
            return
        await buf.throttle.wait()
        if buf.message_id is None:
            buf.message_id = await self.api_client.send_message(
                chat_id=buf.chat_id, text=buf.cumulative_text or "…",
            )
        else:
            await self.api_client.edit_message_text(
                chat_id=buf.chat_id, message_id=buf.message_id,
                text=buf.cumulative_text or "…",
            )

    async def bind_session(self, binding: SessionBinding) -> None:
        await self.bind_session_for_chat(binding.external_thread_id, binding.session_id)

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, status=self._status)  # type: ignore[arg-type]
```

- [x] **Step 4: Implement HttpxTelegramClient**

Create `src/xana/agent_adapter/channels/telegram_client.py`:

```python
"""HttpxTelegramClient: thin Bot API HTTP wrapper."""
from __future__ import annotations

import httpx

from xana.agent_adapter.channels.telegram import TelegramApiClient, TelegramConfig


class HttpxTelegramClient(TelegramApiClient):
    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=f"{config.api_base}/bot{config.bot_token}",
            timeout=httpx.Timeout(connect=10.0, read=config.poll_timeout_s + 5, write=10.0, pool=10.0),
        )

    async def send_message(self, *, chat_id: str, text: str, parse_mode: str | None = None) -> int:
        resp = await self._client.post("/sendMessage",
            json={"chat_id": chat_id, "text": text, **({"parse_mode": parse_mode} if parse_mode else {})})
        data = resp.json()
        return int(data["result"]["message_id"])

    async def edit_message_text(self, *, chat_id: str, message_id: int, text: str,
                                 parse_mode: str | None = None) -> None:
        await self._client.post("/editMessageText", json={
            "chat_id": chat_id, "message_id": message_id, "text": text,
            **({"parse_mode": parse_mode} if parse_mode else {}),
        })

    async def get_updates(self, *, offset: int | None, timeout: int) -> list[dict]:
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        resp = await self._client.get("/getUpdates", params=params)
        return list(resp.json().get("result") or [])
```

- [x] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_telegram_adapter.py -v`
Expected: 3 passed

- [x] **Step 6: Commit**

```bash
git add src/xana/agent_adapter/channels/telegram.py src/xana/agent_adapter/channels/telegram_client.py tests/test_telegram_adapter.py
git commit -m "feat(channels): TelegramAdapter via long-poll + edit-message streaming"
```

---

### Task 7: xana serve command

**Files:**
- Create: `src/xana/cli/serve.py`
- Modify: `src/xana/cli/xana.py`
- Test: `tests/test_serve_command.py`

- [x] **Step 1: Write failing test for the wiring**

Create `tests/test_serve_command.py`:

```python
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from xana.cli.serve import build_adapters_from_env


def test_no_env_returns_empty_list() -> None:
    adapters = build_adapters_from_env(bindings_root=Path("/tmp"), env={})
    assert adapters == []


def test_feishu_env_enables_feishu_adapter(tmp_path: Path) -> None:
    env = {
        "FEISHU_APP_ID": "x", "FEISHU_APP_SECRET": "y",
        "FEISHU_VERIFICATION_TOKEN": "vt",
    }
    adapters = build_adapters_from_env(bindings_root=tmp_path, env=env)
    assert len(adapters) == 1
    assert adapters[0].adapter_id == "feishu"


def test_telegram_env_enables_telegram_adapter(tmp_path: Path) -> None:
    env = {"TELEGRAM_BOT_TOKEN": "t"}
    adapters = build_adapters_from_env(bindings_root=tmp_path, env=env)
    assert len(adapters) == 1
    assert adapters[0].adapter_id == "telegram"


def test_both_envs_enable_both(tmp_path: Path) -> None:
    env = {
        "FEISHU_APP_ID": "x", "FEISHU_APP_SECRET": "y",
        "FEISHU_VERIFICATION_TOKEN": "vt",
        "TELEGRAM_BOT_TOKEN": "t",
    }
    adapters = build_adapters_from_env(bindings_root=tmp_path, env=env)
    ids = {a.adapter_id for a in adapters}
    assert ids == {"feishu", "telegram"}
```

- [x] **Step 2: Run to verify fail**

Run: `pytest tests/test_serve_command.py -v`
Expected: fail — module missing.

- [x] **Step 3: Implement serve.py**

Create `src/xana/cli/serve.py`:

```python
"""xana serve: boot adapters from env, attach to gateway, run forever.

Reads:
  FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_VERIFICATION_TOKEN /
    FEISHU_ENCRYPT_KEY (optional) / FEISHU_WEBHOOK_PORT (default 8765)
  TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_IDS (csv)

Adapters whose required env is missing are skipped silently.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from aiohttp import web

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels.feishu import FeishuAdapter, FeishuConfig
from xana.agent_adapter.channels.feishu_webhook import build_webhook_app
from xana.agent_adapter.channels.telegram import TelegramAdapter, TelegramConfig
from xana.agent_adapter.contracts import InboundMessage
from xana.agent_adapter.interfaces import Adapter
from xana.runtime.boot import boot


def build_adapters_from_env(*, bindings_root: Path, env: dict[str, str]) -> list[Adapter]:
    adapters: list[Adapter] = []
    if all(env.get(k) for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN")):
        cfg = FeishuConfig(
            app_id=env["FEISHU_APP_ID"],
            app_secret=env["FEISHU_APP_SECRET"],
            verification_token=env["FEISHU_VERIFICATION_TOKEN"],
            encrypt_key=env.get("FEISHU_ENCRYPT_KEY") or None,
        )
        bindings = BindingStore(path=bindings_root / "feishu.bindings.json")
        adapters.append(FeishuAdapter(config=cfg, bindings=bindings))
    if env.get("TELEGRAM_BOT_TOKEN"):
        allowed_csv = env.get("TELEGRAM_ALLOWED_USER_IDS", "")
        allowed = frozenset(int(x) for x in allowed_csv.split(",") if x.strip())
        cfg = TelegramConfig(bot_token=env["TELEGRAM_BOT_TOKEN"], allowed_user_ids=allowed)
        bindings = BindingStore(path=bindings_root / "telegram.bindings.json")
        adapters.append(TelegramAdapter(config=cfg, bindings=bindings))
    return adapters


async def _run_inbound_pump(adapter: Adapter, gateway) -> None:
    """Pull inbound messages and forward them through the gateway."""
    async for inbound in adapter.receive():
        async for _rendered in gateway.submit(inbound):
            pass


async def _run(argv: argparse.Namespace) -> int:
    project_root = (argv.project or Path.cwd()).resolve()
    boot_result = await boot(project_root=project_root)
    bindings_root = Path.home() / ".xana" / "channels"
    adapters = build_adapters_from_env(bindings_root=bindings_root, env=dict(os.environ))
    if not adapters:
        print("[xana serve] no adapter env vars set — nothing to run.")
        return 1
    pumps: list[asyncio.Task] = []
    webhook_runner: web.AppRunner | None = None
    for adapter in adapters:
        await adapter.start()
        await boot_result.gateway.register_adapter(adapter.adapter_id, adapter)
        pumps.append(asyncio.create_task(_run_inbound_pump(adapter, boot_result.gateway)))
        if isinstance(adapter, FeishuAdapter):
            app = build_webhook_app(config=adapter.config, on_event=adapter._on_event)
            webhook_runner = web.AppRunner(app)
            await webhook_runner.setup()
            port = int(os.environ.get("FEISHU_WEBHOOK_PORT", "8765"))
            site = web.TCPSite(webhook_runner, "0.0.0.0", port)
            await site.start()
            print(f"[feishu] webhook listening on :{port}/feishu/webhook")
    print(f"[xana serve] running adapters: {', '.join(a.adapter_id for a in adapters)}")
    try:
        await asyncio.gather(*pumps)
    except KeyboardInterrupt:
        pass
    finally:
        if webhook_runner is not None:
            await webhook_runner.cleanup()
        for adapter in adapters:
            await adapter.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xana serve")
    p.add_argument("--project", type=Path, default=None)
    args = p.parse_args(argv)
    return asyncio.run(_run(args))
```

- [x] **Step 4: Register serve subcommand in xana.py**

Read `src/xana/cli/xana.py` and add a `serve` subcommand entry that calls `xana.cli.serve.main`. Subcommand wiring already exists for `setup` and `sync` — follow that pattern.

- [x] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_serve_command.py -v && pytest -q`
Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add src/xana/cli/serve.py src/xana/cli/xana.py tests/test_serve_command.py
git commit -m "feat(cli): xana serve — boot adapters from env and pump inbound to gateway"
```

---

### Task 8: Smoke test against a real bot (optional but recommended)

- [x] **Step 1: Configure Telegram BotFather**

Create a bot via @BotFather, copy the token.

```bash
export TELEGRAM_BOT_TOKEN=...
xana serve
```

Expected: `[xana serve] running adapters: telegram`.

- [x] **Step 2: Smoke test**

DM the bot "hello". Within ~1s, observe the bot's reply streaming in (edited message). After the model finishes, the final message includes a `――――――――――――` divider and italic `_meta_` footer line.

- [x] **Step 3: Cancel cleanly**

Press Ctrl-C. Adapters stop, no leaked tasks.

- [x] **Step 4: Inspect persisted binding**

`cat ~/.xana/channels/telegram.bindings.json` should map the chat_id to a session_id beginning with `tg-`.

- [x] **Step 5: Feishu smoke (only if Feishu app is provisioned)**

Configure Feishu app + bot, expose `:8765/feishu/webhook` to the Internet (ngrok), set `FEISHU_*` env vars, run `xana serve`. Send the bot a DM "hello". Observe the streaming card edit + finalize footer.

---

### Task 9: Regression sweep

- [x] **Step 1: Full pytest**

Run: `pytest -q`
Expected: 0 failures.

- [x] **Step 2: Inspect git log**

Run: `git log --oneline -15`
Expected: 7-8 plan commits.

- [x] **Step 3: Quick lint of new modules**

Run: `python -c "from xana.agent_adapter.channels.feishu import FeishuAdapter; from xana.agent_adapter.channels.telegram import TelegramAdapter; print('ok')"`
Expected: `ok`

---

**End of plan.** Total expected commits: ~8.
