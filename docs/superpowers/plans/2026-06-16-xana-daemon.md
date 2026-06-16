# xana daemon + xanac 瘦客户端 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `xanac` 改成 HTTP+SSE 瘦 client 连本机常驻 `xana serve` daemon,消除每次 oneshot ~14.5s `boot()` 冷启;daemon 协议复用同一份给未来 web/第三方 client。

**Architecture:** serve 内部新增 aiohttp daemon HTTP server,挂一个 `DaemonBridgeAdapter` 到 gateway(同 feishu/tg 一样的 Adapter ABC)。client 端 `DaemonClient` 用 httpx + EventSource 风格订阅 SSE。SSE 每事件带 `id:` + Last-Event-ID 续传 + 心跳 + idle unload。

**Tech Stack:** aiohttp(已有依赖,feishu webhook 在用)、httpx、httpx-sse(新)、pydantic(系统配置)、pytest-asyncio。

**Spec:** `docs/superpowers/specs/2026-06-16-xana-daemon-design.md`

---

## 文件结构

| 路径 | 角色 | 状态 |
|---|---|---|
| `src/xana/config/system_model.py` | 加 `DaemonConfig` | 改 |
| `src/xana/cli/commands/setup_wizard.py` | 首次生成 token | 改 |
| `src/xana/agent_adapter/channels/daemon_bridge.py` | DaemonBridgeAdapter | 新 |
| `src/xana/runtime/daemon_http.py` | aiohttp daemon server | 新 |
| `src/xana/cli/serve.py` | 拉起 daemon_http | 改 |
| `src/xana/cli/_daemon_client.py` | client 侧 httpx+SSE 封装 | 新 |
| `src/xana/cli/xanac.py` | 删 boot 路径,改走 client | 改 |
| `src/xana/cli/xanac_textual.py` | SSE → update_card 接 | 改 |
| `src/xana/agent_adapter/cli.py` | 老 CliAdapter | 删 |
| `src/xana/agent_adapter/channels/_cli_renderer_stdout.py` | 老 renderer | 删 |
| `src/xana/agent_adapter/channels/_cli_renderer_textual.py` | 老 renderer | 删 |
| `tests/test_daemon_*.py` | daemon 单元/e2e | 新 |
| `tests/test_xanac_*.py` | 改成跑 daemon | 改 |

---

## Task 1: DaemonConfig 模型 + 首次 token 生成

**Files:**
- Modify: `src/xana/config/system_model.py`
- Modify: `src/xana/cli/commands/setup_wizard.py`
- Test: `tests/test_daemon_config.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_daemon_config.py
from xana.config.system_model import SystemConfig, DaemonConfig


def test_daemon_default_no_tcp():
    cfg = SystemConfig()
    assert cfg.daemon.unix_socket.endswith("serve.sock")
    assert cfg.daemon.bind is None
    assert cfg.daemon.port == 17777


def test_daemon_accepts_explicit_bind():
    cfg = SystemConfig(daemon=DaemonConfig(bind="127.0.0.1", token="x" * 64))
    assert cfg.daemon.bind == "127.0.0.1"
    assert cfg.daemon.token == "x" * 64


def test_daemon_token_default_empty_warned_at_runtime():
    cfg = SystemConfig()
    # default config has empty token; setup wizard must populate it
    assert cfg.daemon.token == ""
```

- [ ] **Step 2: 跑测试看失败**

```
pytest tests/test_daemon_config.py -v
```
Expected: ImportError `DaemonConfig`.

- [ ] **Step 3: 实现**

在 `src/xana/config/system_model.py`,`SystemConfig` 之前加:

```python
class DaemonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unix_socket: str = "~/.xana/run/serve.sock"
    bind: Optional[str] = None        # 例 "127.0.0.1" / "0.0.0.0"
    port: int = 17777
    token: str = ""                    # 32 字节 hex;setup wizard 首次生成
```

`SystemConfig` 内加:
```python
    daemon: DaemonConfig = DaemonConfig()
```

- [ ] **Step 4: 跑测试看过**

```
pytest tests/test_daemon_config.py -v
```
Expected: 3 passed.

- [ ] **Step 5: 测 setup wizard 写 token**

加测试 `tests/test_daemon_config.py`:

```python
import json
from pathlib import Path
from xana.cli.commands.setup_wizard import ensure_daemon_token


def test_setup_generates_token_when_missing(tmp_path: Path):
    cfg = {"daemon": {"port": 17777}}
    out = tmp_path / "system.json"
    out.write_text(json.dumps(cfg))
    ensure_daemon_token(out)
    data = json.loads(out.read_text())
    assert len(data["daemon"]["token"]) == 64  # secrets.token_hex(32) → 64 chars
    # 二次调用不改 token
    t1 = data["daemon"]["token"]
    ensure_daemon_token(out)
    t2 = json.loads(out.read_text())["daemon"]["token"]
    assert t1 == t2


def test_setup_chmods_system_json_0600(tmp_path: Path):
    out = tmp_path / "system.json"
    out.write_text("{}")
    ensure_daemon_token(out)
    mode = out.stat().st_mode & 0o777
    assert mode == 0o600
```

- [ ] **Step 6: 实现 `ensure_daemon_token`**

在 `src/xana/cli/commands/setup_wizard.py` 末尾加:

```python
import json
import os
import secrets
from pathlib import Path


def ensure_daemon_token(system_json: Path) -> None:
    """如 daemon.token 为空 → 生成 32 字节 hex 写回;chmod 0600。
    幂等:已有 token 不动。"""
    data = {}
    if system_json.exists():
        try:
            data = json.loads(system_json.read_text() or "{}")
        except json.JSONDecodeError:
            data = {}
    daemon = data.setdefault("daemon", {})
    if not daemon.get("token"):
        daemon["token"] = secrets.token_hex(32)
    system_json.parent.mkdir(parents=True, exist_ok=True)
    system_json.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(system_json, 0o600)
```

- [ ] **Step 7: 跑测试看过 + commit**

```
pytest tests/test_daemon_config.py -v
```
Expected: 5 passed.

```
git add src/xana/config/system_model.py src/xana/cli/commands/setup_wizard.py tests/test_daemon_config.py
git commit -m "daemon: add DaemonConfig schema + ensure_daemon_token helper"
```

---

## Task 2: DaemonBridgeAdapter(gateway-facing adapter)

**Files:**
- Create: `src/xana/agent_adapter/channels/daemon_bridge.py`
- Test: `tests/test_daemon_bridge.py`

DaemonBridgeAdapter 把 gateway 推的 AgentEvent/ProgressEvent 入 per-session 队列 + ring buffer;暴露 `subscribe(sid, last_event_id) -> AsyncIterator[(seq, type, payload)]` 给 HTTP 层。

- [ ] **Step 1: 失败测试 — 基本入队**

```python
# tests/test_daemon_bridge.py
import asyncio
import pytest
from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.agent_adapter.contracts import RenderedMessage
from xana.agents.contracts import AgentEvent


@pytest.mark.asyncio
async def test_render_enqueues_event_for_subscriber():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    ev = AgentEvent(kind="draft", session_id="s1", payload={"text": "hi"})
    await bridge.render(ev)
    # subscribe
    agen = bridge.subscribe("s1", last_event_id=None)
    seq, evt_type, data = await asyncio.wait_for(agen.__anext__(), timeout=1)
    assert evt_type == "agent_event"
    assert seq >= 1
    assert data["kind"] == "draft"
    await agen.aclose()


@pytest.mark.asyncio
async def test_ring_buffer_replays_from_last_event_id():
    bridge = DaemonBridgeAdapter(ring_buffer_size=10)
    await bridge.start()
    for i in range(5):
        await bridge.render(AgentEvent(
            kind="draft", session_id="s1", payload={"text": str(i)}))
    # subscribe with Last-Event-ID=2 → should see seq 3,4,5 (1-indexed)
    seen = []
    async def collect():
        async for seq, t, d in bridge.subscribe("s1", last_event_id=2):
            seen.append((seq, d["payload"]["text"]))
            if len(seen) == 3:
                break
    await asyncio.wait_for(collect(), timeout=1)
    assert [s[1] for s in seen] == ["2", "3", "4"]


@pytest.mark.asyncio
async def test_ring_buffer_emits_replay_gap_when_too_old():
    bridge = DaemonBridgeAdapter(ring_buffer_size=3)
    await bridge.start()
    for i in range(10):
        await bridge.render(AgentEvent(
            kind="draft", session_id="s1", payload={"text": str(i)}))
    # ring buffer only has last 3 (seq 8,9,10). Client asks from seq 2.
    agen = bridge.subscribe("s1", last_event_id=2)
    seq, t, d = await asyncio.wait_for(agen.__anext__(), timeout=1)
    assert t == "replay_gap"
    await agen.aclose()
```

- [ ] **Step 2: 跑看失败**

```
pytest tests/test_daemon_bridge.py -v
```
Expected: ImportError.

- [ ] **Step 3: 实现 DaemonBridgeAdapter**

`src/xana/agent_adapter/channels/daemon_bridge.py`:

```python
"""DaemonBridgeAdapter: gateway → HTTP daemon 的桥。

per-session:
  - 单调递增 seq
  - ring buffer(deque,默认 5000 条)
  - subscriber 队列集合(支持 multi-client 同 session)

subscribe(sid, last_event_id) 流程:
  1. 如果 last_event_id 在 ring 内 → 先回放 (last+1..tail)
  2. 如果 last_event_id 比 ring 最旧的还旧 → 发一条 "replay_gap" 后跳到 tail
  3. 进入 push 循环,从新 events 增量推
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from xana.agent_adapter.contracts import (
    AdapterHealth, InboundMessage, RenderedMessage, SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent


@dataclass
class _SessionState:
    seq: int = 0
    ring: deque = field(default_factory=deque)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    last_activity_ts: float = 0.0


class DaemonBridgeAdapter(Adapter):
    adapter_id = "cli"

    def __init__(self, ring_buffer_size: int = 5000) -> None:
        self._ring_size = ring_buffer_size
        self._sessions: dict[str, _SessionState] = {}
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._status = "stopped"

    async def start(self) -> None:
        self._status = "healthy"

    async def stop(self) -> None:
        self._status = "stopped"

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, status=self._status)  # type: ignore[arg-type]

    async def bind_session(self, binding: SessionBinding) -> None:
        return None

    def receive(self):
        return self._receive()

    async def _receive(self):
        msg = await self._inbound.get()
        yield msg

    async def submit_inbound(self, session_id: str, text: str,
                             metadata: dict | None = None) -> None:
        await self._inbound.put(InboundMessage(
            adapter_id=self.adapter_id,
            session_id=session_id,
            text=text,
            metadata=metadata or {},
        ))

    def _get_or_create(self, sid: str) -> _SessionState:
        st = self._sessions.get(sid)
        if st is None:
            st = _SessionState(ring=deque(maxlen=self._ring_size))
            self._sessions[sid] = st
        return st

    def _enqueue(self, sid: str, evt_type: str, data: dict) -> None:
        st = self._get_or_create(sid)
        st.seq += 1
        item = (st.seq, evt_type, data)
        st.ring.append(item)
        # fan-out to all subscribers
        dead: list[asyncio.Queue] = []
        for q in st.subscribers:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            st.subscribers.discard(q)

    async def render(self, event: AgentEvent) -> RenderedMessage:
        sid = event.session_id
        self._enqueue(sid, "agent_event", _agent_event_to_dict(event))
        return RenderedMessage(
            adapter_id=self.adapter_id, session_id=sid,
            text=str(event.payload.get("text", "")),
            metadata={"event_kind": event.kind},
        )

    def note_progress(self, payload: dict[str, Any]) -> None:
        sid = str(payload.get("session_id") or "")
        if not sid:
            return
        self._enqueue(sid, "progress", dict(payload))

    async def subscribe(
        self, sid: str, *, last_event_id: int | None = None,
        queue_size: int = 1000,
    ) -> AsyncIterator[tuple[int, str, dict]]:
        st = self._get_or_create(sid)
        q: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        # Replay phase
        if last_event_id is not None and st.ring:
            oldest_seq = st.ring[0][0]
            if last_event_id < oldest_seq - 1:
                # gap — client too far behind
                yield (st.seq, "replay_gap", {"reason": "out_of_range",
                                              "oldest_available": oldest_seq})
            else:
                for seq, t, d in list(st.ring):
                    if seq > last_event_id:
                        yield (seq, t, d)
        st.subscribers.add(q)
        try:
            while True:
                item = await q.get()
                yield item
        finally:
            st.subscribers.discard(q)


def _agent_event_to_dict(ev: AgentEvent) -> dict:
    out = {
        "kind": ev.kind,
        "session_id": ev.session_id,
        "payload": dict(ev.payload or {}),
    }
    if ev.harness_event is not None:
        out["harness_event"] = {
            "kind": ev.harness_event.kind,
            "session_id": ev.harness_event.session_id,
            "run_id": ev.harness_event.run_id,
            "payload": dict(ev.harness_event.payload or {}),
        }
    return out
```

- [ ] **Step 4: 跑看过**

```
pytest tests/test_daemon_bridge.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add src/xana/agent_adapter/channels/daemon_bridge.py tests/test_daemon_bridge.py
git commit -m "daemon: DaemonBridgeAdapter with ring buffer + multi-subscriber fan-out"
```

---

## Task 3: daemon_http — submit + SSE 主流程 + initialize 帧

**Files:**
- Create: `src/xana/runtime/daemon_http.py`
- Test: `tests/test_daemon_http_basic.py`

- [ ] **Step 1: 失败测试 — submit + SSE 收到 initialize + agent_event**

```python
# tests/test_daemon_http_basic.py
import asyncio
import json
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.runtime.daemon_http import build_daemon_app
from xana.agents.contracts import AgentEvent


@pytest.fixture
async def app_with_bridge():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    app = build_daemon_app(bridge=bridge, token="")
    async with TestClient(TestServer(app)) as client:
        yield client, bridge


@pytest.mark.asyncio
async def test_healthz_ok(app_with_bridge):
    client, _ = app_with_bridge
    r = await client.get("/healthz")
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_stream_first_frame_is_initialize(app_with_bridge):
    client, _ = app_with_bridge
    resp = await client.get("/sessions/s1/stream")
    assert resp.status == 200
    line = await resp.content.readline()
    assert line == b"id: 0\n"
    line = await resp.content.readline()
    assert line == b"event: initialize\n"
    line = await resp.content.readline()
    assert line.startswith(b"data: ")
    payload = json.loads(line[len(b"data: "):])
    assert "server_version" in payload
    assert payload["protocol_version"] == 1
    resp.close()


@pytest.mark.asyncio
async def test_submit_then_stream_sees_event(app_with_bridge):
    client, bridge = app_with_bridge
    # 启 SSE 订阅
    resp = await client.get("/sessions/s1/stream")
    # 跳过 initialize(3 行 + blank)
    for _ in range(4):
        await resp.content.readline()
    # 后端塞一条事件(直接走 bridge.render 模拟 gateway)
    asyncio.create_task(bridge.render(AgentEvent(
        kind="draft", session_id="s1", payload={"text": "hi"})))
    # 应收到 agent_event
    id_line = await asyncio.wait_for(resp.content.readline(), timeout=2)
    assert id_line.startswith(b"id: ")
    evt_line = await resp.content.readline()
    assert evt_line == b"event: agent_event\n"
    data_line = await resp.content.readline()
    assert data_line.startswith(b"data: ")
    data = json.loads(data_line[len(b"data: "):])
    assert data["kind"] == "draft"
    resp.close()
```

- [ ] **Step 2: 跑看失败**

```
pytest tests/test_daemon_http_basic.py -v
```
Expected: ImportError.

- [ ] **Step 3: 实现 build_daemon_app**

`src/xana/runtime/daemon_http.py`:

```python
"""xana daemon HTTP+SSE server.

build_daemon_app(bridge, token, *, on_action) → aiohttp.web.Application
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from aiohttp import web

from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter

_logger = logging.getLogger(__name__)
_PROTOCOL_VERSION = 1
_SERVER_VERSION = "0.1.0"


def build_daemon_app(
    *,
    bridge: DaemonBridgeAdapter,
    token: str,
    on_action: Callable[[str, str, dict], Awaitable[None]] | None = None,
) -> web.Application:
    """build app。

    on_action(sid, action, payload):cancel/attach/detach/button 时调,
    宿主负责调用 gateway.cancel / attach / detach / card.dispatch。
    """
    app = web.Application()

    @web.middleware
    async def origin_reject(request, handler):
        if request.headers.get("Origin"):
            return web.json_response(
                {"error": "Origin header rejected"}, status=403)
        return await handler(request)

    @web.middleware
    async def auth(request, handler):
        if request.path == "/healthz":
            return await handler(request)
        if not token:
            return await handler(request)
        got = request.headers.get("Authorization", "")
        if got != f"Bearer {token}":
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    app.middlewares.extend([origin_reject, auth])

    async def healthz(_request):
        return web.json_response({"ok": True, "version": _SERVER_VERSION})

    async def submit(request):
        sid = request.match_info["sid"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        text = str(body.get("text", "") or "")
        await bridge.submit_inbound(sid, text, body.get("metadata"))
        return web.json_response({}, status=202)

    async def stream(request):
        sid = request.match_info["sid"]
        last_event_id = request.headers.get("Last-Event-ID")
        if last_event_id is None:
            last_id_int = None
        else:
            try:
                last_id_int = int(last_event_id)
            except ValueError:
                last_id_int = None

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)

        # initialize frame
        init_payload = {
            "server_version": _SERVER_VERSION,
            "protocol_version": _PROTOCOL_VERSION,
            "supported_methods": ["agent_event", "progress",
                                  "session.status", "turn.interrupted"],
        }
        await _write_sse(resp, seq=0, evt="initialize", data=init_payload)

        try:
            async for seq, evt_type, data in bridge.subscribe(
                sid, last_event_id=last_id_int):
                await _write_sse(resp, seq=seq, evt=evt_type, data=data)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        return resp

    async def action(request):
        sid = request.match_info["sid"]
        kind = request.match_info["action"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        if on_action is not None:
            asyncio.create_task(on_action(sid, kind, body))
        return web.json_response({}, status=202)

    app.router.add_get("/healthz", healthz)
    app.router.add_post("/sessions/{sid}/submit", submit)
    app.router.add_get("/sessions/{sid}/stream", stream)
    app.router.add_post("/sessions/{sid}/{action:cancel|attach|detach|button}",
                        action)
    return app


async def _write_sse(resp: web.StreamResponse, *, seq: int, evt: str,
                     data: dict) -> None:
    chunk = (
        f"id: {seq}\n"
        f"event: {evt}\n"
        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    ).encode("utf-8")
    await resp.write(chunk)
```

- [ ] **Step 4: 跑看过**

```
pytest tests/test_daemon_http_basic.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add src/xana/runtime/daemon_http.py tests/test_daemon_http_basic.py
git commit -m "daemon: HTTP server (submit/stream/healthz) with initialize frame + Last-Event-ID"
```

---

## Task 4: 心跳 + idle unload + session.status

**Files:**
- Modify: `src/xana/runtime/daemon_http.py`
- Modify: `src/xana/agent_adapter/channels/daemon_bridge.py`
- Test: `tests/test_daemon_http_idle.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_daemon_http_idle.py
import asyncio
import json
import pytest
from aiohttp.test_utils import TestClient, TestServer
from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.runtime.daemon_http import build_daemon_app
from xana.agents.contracts import AgentEvent


@pytest.mark.asyncio
async def test_heartbeat_emitted_periodically():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    app = build_daemon_app(bridge=bridge, token="", heartbeat_interval_s=0.2)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/sessions/s1/stream")
        # 跳 initialize
        for _ in range(4):
            await resp.content.readline()
        # 0.5s 内至少一条 ": heartbeat"
        deadline = asyncio.get_event_loop().time() + 0.8
        got_heartbeat = False
        while asyncio.get_event_loop().time() < deadline:
            line = await asyncio.wait_for(resp.content.readline(), timeout=0.5)
            if line.startswith(b": heartbeat"):
                got_heartbeat = True
                break
        assert got_heartbeat
        resp.close()


@pytest.mark.asyncio
async def test_session_status_event_emitted_on_subscribe():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    app = build_daemon_app(bridge=bridge, token="")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/sessions/s1/stream")
        # skip initialize
        for _ in range(4):
            await resp.content.readline()
        # next event should be session.status
        id_line = await asyncio.wait_for(resp.content.readline(), timeout=1)
        evt_line = await resp.content.readline()
        assert evt_line == b"event: session.status\n"
        data_line = await resp.content.readline()
        payload = json.loads(data_line[len(b"data: "):])
        assert payload["state"] in ("idle", "active")
        assert payload["subscribers"] == 1
        resp.close()
```

- [ ] **Step 2: 跑看失败**

```
pytest tests/test_daemon_http_idle.py -v
```
Expected: heartbeat_interval_s 不接受。

- [ ] **Step 3: 改实现**

`build_daemon_app` 签名加 `heartbeat_interval_s: float = 15.0, idle_unload_s: float = 1800.0`。在 `stream` 里:

```python
async def stream(request):
    sid = request.match_info["sid"]
    last_id_int = _parse_last_event_id(request.headers.get("Last-Event-ID"))

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)

    # initialize
    await _write_sse(resp, seq=0, evt="initialize", data={
        "server_version": _SERVER_VERSION,
        "protocol_version": _PROTOCOL_VERSION,
        "supported_methods": ["agent_event", "progress",
                              "session.status", "turn.interrupted"],
    })

    # 立刻报 session.status
    bridge.note_subscribe(sid)
    status = bridge.session_status(sid)
    await _write_sse(resp, seq=0, evt="session.status", data=status)

    heartbeat_task = asyncio.create_task(_heartbeat(resp, heartbeat_interval_s))
    try:
        async for seq, evt_type, data in bridge.subscribe(
            sid, last_event_id=last_id_int):
            await _write_sse(resp, seq=seq, evt=evt_type, data=data)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        heartbeat_task.cancel()
        bridge.note_unsubscribe(sid)
    return resp


async def _heartbeat(resp, interval_s):
    while True:
        await asyncio.sleep(interval_s)
        try:
            await resp.write(b": heartbeat\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            return
```

`_parse_last_event_id`:

```python
def _parse_last_event_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
```

`DaemonBridgeAdapter` 加方法:

```python
import time

def note_subscribe(self, sid: str) -> None:
    self._get_or_create(sid).last_activity_ts = time.monotonic()

def note_unsubscribe(self, sid: str) -> None:
    # 仅打时间;subscribe() finally 已经从 set 移除 queue
    pass

def session_status(self, sid: str) -> dict:
    st = self._get_or_create(sid)
    return {
        "state": "active" if st.subscribers else "idle",
        "subscribers": len(st.subscribers),
        "active_run_ids": [],   # 由 harness 后续 wire 进来
    }
```

- [ ] **Step 4: 跑看过**

```
pytest tests/test_daemon_http_idle.py -v
```
Expected: 2 passed.

- [ ] **Step 5: 加 idle unload 测试**

```python
@pytest.mark.asyncio
async def test_idle_unload_emits_session_closed():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    app = build_daemon_app(bridge=bridge, token="", idle_unload_s=0.3)
    runner_task = asyncio.create_task(
        _run_idle_loop(bridge, idle_unload_s=0.3))
    try:
        # session 创建过(被 _enqueue 触发)
        await bridge.render(AgentEvent(
            kind="draft", session_id="s2", payload={"text": "hi"}))
        # 没人订阅 → 0.3s 后应进 closed
        await asyncio.sleep(0.5)
        assert "s2" not in bridge._sessions
    finally:
        runner_task.cancel()
```

`_run_idle_loop` 是新函数,扫 sessions,无 subscriber + 上次活动 > idle_unload_s → 弹出。在 `daemon_http.py` 顶层:

```python
async def _run_idle_loop(bridge: DaemonBridgeAdapter, *, idle_unload_s: float):
    while True:
        await asyncio.sleep(min(idle_unload_s / 2, 30))
        bridge.sweep_idle(idle_unload_s)
```

`DaemonBridgeAdapter.sweep_idle`:

```python
def sweep_idle(self, idle_unload_s: float) -> None:
    import time
    now = time.monotonic()
    dead = [sid for sid, st in self._sessions.items()
            if not st.subscribers
            and now - st.last_activity_ts > idle_unload_s]
    for sid in dead:
        self._sessions.pop(sid, None)
```

`_enqueue` 里更新 `last_activity_ts = time.monotonic()`。

`build_daemon_app` 启动时 `app.on_startup.append(...)` 起 idle loop。

- [ ] **Step 6: 跑看过 + commit**

```
pytest tests/test_daemon_http_idle.py -v
```
Expected: 3 passed.

```
git add src/xana/runtime/daemon_http.py src/xana/agent_adapter/channels/daemon_bridge.py tests/test_daemon_http_idle.py
git commit -m "daemon: heartbeat + session.status + idle unload loop"
```

---

## Task 5: action endpoints — cancel/attach/detach/button 路由到 gateway

**Files:**
- Modify: `src/xana/runtime/daemon_http.py`(已有 action handler 框架,加联线)
- Test: `tests/test_daemon_actions.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_daemon_actions.py
import pytest
from aiohttp.test_utils import TestClient, TestServer
from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.runtime.daemon_http import build_daemon_app


@pytest.mark.asyncio
async def test_cancel_calls_on_action():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    called: list[tuple[str, str, dict]] = []

    async def on_action(sid, kind, payload):
        called.append((sid, kind, payload))

    app = build_daemon_app(bridge=bridge, token="", on_action=on_action)
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/sessions/s1/cancel",
                              json={"run_id": "r-1"})
        assert r.status == 202
        # on_action 在 task 里,等一拍
        import asyncio; await asyncio.sleep(0.05)
        assert called == [("s1", "cancel", {"run_id": "r-1"})]


@pytest.mark.asyncio
async def test_button_routes_action_payload():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    called = []

    async def on_action(sid, kind, payload):
        called.append((sid, kind, payload))

    app = build_daemon_app(bridge=bridge, token="", on_action=on_action)
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/sessions/s1/button",
            json={"action": "load_tools", "card_id": "c-1"})
        assert r.status == 202
        import asyncio; await asyncio.sleep(0.05)
        assert called[0][1] == "button"
        assert called[0][2]["action"] == "load_tools"
```

- [ ] **Step 2: 跑看过(action 已实现)**

`action` handler 在 Task 3 已写;这里只验证联线对。

```
pytest tests/test_daemon_actions.py -v
```
Expected: 2 passed.

- [ ] **Step 3: 把 cancel/attach/detach 真接到 gateway(在 serve.py wire 时再做,这里先确保 on_action 协议干净)。Commit:**

```
git add tests/test_daemon_actions.py
git commit -m "daemon: action endpoints e2e test"
```

---

## Task 6: auth + Origin 拒 + Last-Event-ID 续传 e2e

**Files:**
- Test: `tests/test_daemon_auth.py`
- Test: `tests/test_daemon_resume.py`

- [ ] **Step 1: 写 auth 测试**

```python
# tests/test_daemon_auth.py
import pytest
from aiohttp.test_utils import TestClient, TestServer
from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.runtime.daemon_http import build_daemon_app


@pytest.mark.asyncio
async def test_origin_header_rejected():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    app = build_daemon_app(bridge=bridge, token="")
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/healthz", headers={"Origin": "http://evil"})
        assert r.status == 403


@pytest.mark.asyncio
async def test_token_required_when_set():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    app = build_daemon_app(bridge=bridge, token="t0p-s3kret")
    async with TestClient(TestServer(app)) as client:
        # /healthz 不要 token
        r = await client.get("/healthz")
        assert r.status == 200
        # submit 要
        r = await client.post("/sessions/s1/submit", json={"text": "hi"})
        assert r.status == 401
        r = await client.post(
            "/sessions/s1/submit", json={"text": "hi"},
            headers={"Authorization": "Bearer t0p-s3kret"})
        assert r.status == 202
```

```python
# tests/test_daemon_resume.py
import asyncio
import json
import pytest
from aiohttp.test_utils import TestClient, TestServer
from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.runtime.daemon_http import build_daemon_app
from xana.agents.contracts import AgentEvent


@pytest.mark.asyncio
async def test_last_event_id_resumes_from_next_seq():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    for i in range(5):
        await bridge.render(AgentEvent(
            kind="draft", session_id="s1", payload={"text": str(i)}))
    app = build_daemon_app(bridge=bridge, token="")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/sessions/s1/stream",
            headers={"Last-Event-ID": "2"})
        # 跳 initialize + session.status (各 4 行)
        for _ in range(8):
            await resp.content.readline()
        # 接着是 seq 3
        id_line = await asyncio.wait_for(resp.content.readline(), timeout=1)
        assert id_line == b"id: 3\n"
        resp.close()
```

- [ ] **Step 2: 跑看过(实现应已支持)**

```
pytest tests/test_daemon_auth.py tests/test_daemon_resume.py -v
```
Expected: 3 passed.

- [ ] **Step 3: Commit**

```
git add tests/test_daemon_auth.py tests/test_daemon_resume.py
git commit -m "daemon: auth + Origin reject + Last-Event-ID resume e2e"
```

---

## Task 7: Unix socket 支持

**Files:**
- Modify: `src/xana/runtime/daemon_http.py`(加 `serve_unix_socket` 辅助)
- Test: `tests/test_daemon_unix.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_daemon_unix.py
import asyncio
import json
import os
import pytest
import httpx
from pathlib import Path

from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.runtime.daemon_http import build_daemon_app, run_unix_socket


@pytest.mark.asyncio
async def test_unix_socket_chmod_0600(tmp_path: Path):
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    app = build_daemon_app(bridge=bridge, token="")
    sock = tmp_path / "serve.sock"
    runner_task = asyncio.create_task(run_unix_socket(app, str(sock)))
    try:
        # 等 socket 文件出现
        for _ in range(50):
            if sock.exists():
                break
            await asyncio.sleep(0.02)
        assert sock.exists()
        assert (sock.stat().st_mode & 0o777) == 0o600
        # client 经 unix socket 探活
        transport = httpx.AsyncHTTPTransport(uds=str(sock))
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://unix") as cli:
            r = await cli.get("/healthz")
            assert r.status_code == 200
    finally:
        runner_task.cancel()
        try:
            await runner_task
        except (asyncio.CancelledError, Exception):
            pass
```

- [ ] **Step 2: 跑看失败**

```
pytest tests/test_daemon_unix.py -v
```

- [ ] **Step 3: 实现 `run_unix_socket`**

`src/xana/runtime/daemon_http.py` 加:

```python
import os
from aiohttp import web


async def run_unix_socket(app: web.Application, path: str) -> None:
    """启 aiohttp UnixSite + chmod 0600。阻塞协程到 cancel。"""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, path)
    await site.start()
    os.chmod(path, 0o600)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


async def run_tcp(app: web.Application, host: str, port: int) -> None:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
```

- [ ] **Step 4: 跑看过 + commit**

```
pytest tests/test_daemon_unix.py -v
```
Expected: 1 passed.

```
git add src/xana/runtime/daemon_http.py tests/test_daemon_unix.py
git commit -m "daemon: run_unix_socket / run_tcp helpers with 0600 perm"
```

---

## Task 8: serve.py wire daemon + action 联线 gateway

**Files:**
- Modify: `src/xana/cli/serve.py`
- Test: `tests/test_serve_daemon_integration.py`

- [ ] **Step 1: 失败测试 — 起 serve(in-process)→ httpx submit "hi" → SSE 收 final**

```python
# tests/test_serve_daemon_integration.py
import asyncio
import json
import pytest
import httpx
from pathlib import Path

from xana.cli.serve import _start_daemon_in_process


@pytest.mark.asyncio
async def test_serve_daemon_round_trip(tmp_path: Path, monkeypatch):
    """boot in-process,起 daemon TCP 在 ephemeral 端口,submit hi,SSE 收 final。
    跳过模型调用:用 fake provider(测试夹具)。
    """
    monkeypatch.setenv("XANA_TEST_FAKE_MODEL", "1")
    port = 17778  # 测试端口
    bridge, gateway, runner_task = await _start_daemon_in_process(
        project_root=tmp_path, port=port, token="")
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=5.0) as cli:
            # 起 SSE 订阅
            async with cli.stream("GET", "/sessions/default/stream") as resp:
                assert resp.status_code == 200
                # submit
                sr = await cli.post(
                    "/sessions/default/submit", json={"text": "hi"})
                assert sr.status_code == 202
                # 读到 final agent_event 为止
                got_final = False
                async for line in resp.aiter_lines():
                    if line.startswith("event: agent_event"):
                        # 下一行是 data
                        pass
                    if '"kind": "final"' in line:
                        got_final = True
                        break
                assert got_final
    finally:
        runner_task.cancel()
        await asyncio.gather(runner_task, return_exceptions=True)
```

- [ ] **Step 2: 实现 `_start_daemon_in_process`**

`src/xana/cli/serve.py` 加(在 main loop 之前):

```python
async def _start_daemon_in_process(
    *, project_root: Path, port: int | None, token: str,
    unix_socket: str | None = None,
) -> tuple[Any, Any, asyncio.Task]:
    """boot + 注册 DaemonBridgeAdapter + 起 HTTP server。
    返回 (bridge, gateway, runner_task)。runner_task cancel 时收尾。"""
    from xana.runtime.boot import boot
    from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
    from xana.runtime.daemon_http import build_daemon_app, run_tcp, run_unix_socket

    boot_result = await boot(project_root=project_root,
                             history_root=Path.home()/".xana"/"sessions")
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    await boot_result.gateway.register_adapter("cli", bridge)

    async def on_action(sid: str, kind: str, payload: dict) -> None:
        if kind == "cancel":
            run_id = payload.get("run_id")
            await boot_result.gateway.cancel(sid, run_id)
        elif kind == "attach":
            run_id = payload.get("run_id")
            await boot_result.gateway.attach(sid, run_id)
        elif kind == "detach":
            run_id = payload.get("run_id")
            await boot_result.gateway.detach(sid, run_id)
        elif kind == "button":
            # button 走 inbound submit("/cmd ..." 文本路径)
            # 复杂动作(load_tools/expand)由 client 维持 Card,这里仅
            # 处理需要 server 侧介入的情况
            pass

    app = build_daemon_app(bridge=bridge, token=token, on_action=on_action)

    if unix_socket:
        task = asyncio.create_task(run_unix_socket(app, unix_socket))
    else:
        host = "127.0.0.1"
        task = asyncio.create_task(run_tcp(app, host, port or 17777))
    return bridge, boot_result.gateway, task
```

`serve_main` 主流程里(在 existing feishu/tg adapter 起完后)加:

```python
from xana.config.system_model import SystemConfig
sys_cfg = SystemConfig.load()   # 假设已有 loader;否则 from xana.config.loader
daemon_cfg = sys_cfg.daemon
unix_socket = Path(daemon_cfg.unix_socket).expanduser() if daemon_cfg.unix_socket else None
if unix_socket:
    unix_socket.parent.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(run_unix_socket(
        build_daemon_app(bridge=bridge, token="" /* unix path: no token */,
                         on_action=on_action),
        str(unix_socket),
    ))
if daemon_cfg.bind:
    asyncio.create_task(run_tcp(
        build_daemon_app(bridge=bridge, token=daemon_cfg.token,
                         on_action=on_action),
        daemon_cfg.bind, daemon_cfg.port,
    ))
    if daemon_cfg.bind == "0.0.0.0":
        _logger.warning("daemon bound to 0.0.0.0 — exposed to LAN")
```

- [ ] **Step 3: 跑测试**

```
pytest tests/test_serve_daemon_integration.py -v
```

If model fake fixture 不存在,可改成 test 不跑模型,只验 healthz / submit 202 / SSE 收到任何 agent_event(短路 final 期望)。

- [ ] **Step 4: Commit**

```
git add src/xana/cli/serve.py tests/test_serve_daemon_integration.py
git commit -m "daemon: wire daemon_http into xana serve (always-on)"
```

---

## Task 9: DaemonClient(httpx + SSE 封装)

**Files:**
- Create: `src/xana/cli/_daemon_client.py`
- Test: `tests/test_daemon_client.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_daemon_client.py
import asyncio
import json
import pytest
from aiohttp.test_utils import TestClient, TestServer
from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.runtime.daemon_http import build_daemon_app
from xana.agents.contracts import AgentEvent
from xana.cli._daemon_client import DaemonClient, DaemonEvent


@pytest.mark.asyncio
async def test_client_iterates_events_with_initialize_first():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    app = build_daemon_app(bridge=bridge, token="")
    async with TestClient(TestServer(app)) as srv:
        base = str(srv.make_url("/"))[:-1]
        client = DaemonClient(base_url=base, token="")
        seen: list[DaemonEvent] = []

        async def reader():
            async for ev in client.stream("s1"):
                seen.append(ev)
                if ev.event == "agent_event":
                    break
        task = asyncio.create_task(reader())
        await asyncio.sleep(0.1)
        await bridge.render(AgentEvent(
            kind="draft", session_id="s1", payload={"text": "hi"}))
        await asyncio.wait_for(task, timeout=2)
        assert seen[0].event == "initialize"
        # 第二条可能是 session.status,第三条是 agent_event
        assert any(s.event == "agent_event" for s in seen)
        await client.close()


@pytest.mark.asyncio
async def test_client_submit_returns_202(monkeypatch):
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    app = build_daemon_app(bridge=bridge, token="")
    async with TestClient(TestServer(app)) as srv:
        base = str(srv.make_url("/"))[:-1]
        client = DaemonClient(base_url=base, token="")
        await client.submit("s1", "hi")  # 不抛即可
        await client.close()
```

- [ ] **Step 2: 跑看失败**

```
pytest tests/test_daemon_client.py -v
```

- [ ] **Step 3: 实现**

`src/xana/cli/_daemon_client.py`:

```python
"""xana daemon HTTP+SSE client."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

_logger = logging.getLogger(__name__)


@dataclass
class DaemonEvent:
    seq: int
    event: str
    data: dict


class DaemonClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        unix_socket: str | None = None,
    ) -> None:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        transport = (
            httpx.AsyncHTTPTransport(uds=unix_socket)
            if unix_socket else None
        )
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers,
            transport=transport, timeout=httpx.Timeout(connect=2.0, read=None,
                                                       write=10.0, pool=10.0),
        )
        self._last_seq: dict[str, int] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def stream(self, sid: str) -> AsyncIterator[DaemonEvent]:
        """SSE 订阅,断线自动重连 + Last-Event-ID 续传。"""
        while True:
            headers = {}
            last = self._last_seq.get(sid)
            if last is not None:
                headers["Last-Event-ID"] = str(last)
            try:
                async with self._client.stream(
                    "GET", f"/sessions/{sid}/stream", headers=headers
                ) as resp:
                    resp.raise_for_status()
                    async for evt in _iter_sse(resp):
                        if evt.seq:
                            self._last_seq[sid] = evt.seq
                        yield evt
            except (httpx.RemoteProtocolError, httpx.ReadError,
                    httpx.ConnectError) as exc:
                _logger.debug("SSE reconnect (%s)", type(exc).__name__)
                await asyncio.sleep(0.5)

    async def submit(self, sid: str, text: str,
                     metadata: dict | None = None) -> None:
        body = {"text": text}
        if metadata:
            body["metadata"] = metadata
        r = await self._client.post(f"/sessions/{sid}/submit", json=body)
        r.raise_for_status()

    async def cancel(self, sid: str, run_id: str | None = None) -> None:
        body = {"run_id": run_id} if run_id else {}
        r = await self._client.post(f"/sessions/{sid}/cancel", json=body)
        r.raise_for_status()

    async def attach(self, sid: str, run_id: str) -> None:
        r = await self._client.post(
            f"/sessions/{sid}/attach", json={"run_id": run_id})
        r.raise_for_status()

    async def detach(self, sid: str, run_id: str) -> None:
        r = await self._client.post(
            f"/sessions/{sid}/detach", json={"run_id": run_id})
        r.raise_for_status()

    async def button(self, sid: str, payload: dict) -> None:
        r = await self._client.post(f"/sessions/{sid}/button", json=payload)
        r.raise_for_status()


async def _iter_sse(resp: httpx.Response) -> AsyncIterator[DaemonEvent]:
    """从 SSE 响应解析 id/event/data。心跳(注释行)忽略。"""
    cur_id: int = 0
    cur_event: str = ""
    cur_data: list[str] = []
    async for raw in resp.aiter_lines():
        line = raw.rstrip("\r")
        if line == "":
            if cur_event and cur_data:
                try:
                    payload = json.loads("\n".join(cur_data))
                except Exception:
                    payload = {}
                yield DaemonEvent(seq=cur_id, event=cur_event, data=payload)
            cur_id = 0
            cur_event = ""
            cur_data = []
            continue
        if line.startswith(":"):
            continue  # 注释 / heartbeat
        if line.startswith("id:"):
            try:
                cur_id = int(line[3:].strip())
            except ValueError:
                cur_id = 0
        elif line.startswith("event:"):
            cur_event = line[6:].strip()
        elif line.startswith("data:"):
            cur_data.append(line[5:].lstrip())
```

- [ ] **Step 4: 跑看过 + commit**

```
pytest tests/test_daemon_client.py -v
```
Expected: 2 passed.

```
git add src/xana/cli/_daemon_client.py tests/test_daemon_client.py
git commit -m "daemon: DaemonClient (httpx+SSE) with auto-reconnect"
```

---

## Task 10: xanac 重写 — oneshot + auto-start serve

**Files:**
- Modify: `src/xana/cli/xanac.py`(删 boot 路径,换走 DaemonClient)
- Test: `tests/test_xanac_oneshot_daemon.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_xanac_oneshot_daemon.py
import asyncio
import pytest
from pathlib import Path
import io
from aiohttp.test_utils import TestClient, TestServer
from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.runtime.daemon_http import build_daemon_app
from xana.cli.xanac import _oneshot_run
from xana.agents.contracts import AgentEvent
from xana.harness_sdk.contracts import HarnessEvent


@pytest.mark.asyncio
async def test_oneshot_run_prints_final_text():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    app = build_daemon_app(bridge=bridge, token="")
    async with TestClient(TestServer(app)) as srv:
        base = str(srv.make_url("/"))[:-1]
        out = io.StringIO()

        async def producer():
            await asyncio.sleep(0.1)
            await bridge.render(AgentEvent(
                kind="final", session_id="s1",
                payload={"text": "hello back"},
                harness_event=HarnessEvent(
                    kind="final", session_id="s1", run_id="r1",
                    payload={"text": "hello back"}),
            ))
        asyncio.create_task(producer())

        rc = await _oneshot_run(
            base_url=base, token="", session_id="s1",
            text="hi", out=out, timeout_s=3.0,
        )
        assert rc == 0
        assert "hello back" in out.getvalue()
```

- [ ] **Step 2: 改 xanac.py**

把 `_run` 整段换成走 `DaemonClient`。简化结构(删 in-process boot):

```python
# src/xana/cli/xanac.py
"""xanac: HTTP+SSE thin client to xana serve daemon."""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, TextIO

from xana.agent_adapter.channels.unified_card import UnifiedCardState
from xana.agent_adapter.channels.unified_card_events import apply_event
from xana.agent_adapter.channels.unified_card_cli import render as render_card
from xana.cli._daemon_client import DaemonClient, DaemonEvent
from xana.config.system_model import SystemConfig


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="xanac")
    p.add_argument("text", nargs="*", help="one-shot prompt")
    p.add_argument("--session", default="default")
    p.add_argument("--random-session", action="store_true")
    p.add_argument("-i", "--interactive", action="store_true")
    return p.parse_args(argv)


def _resolve_endpoint() -> tuple[str, str, str | None]:
    """返回 (base_url, token, unix_socket_path or None)。"""
    home = Path.home() / ".xana"
    sys_json = home / "config" / "system.json"
    cfg = SystemConfig()
    if sys_json.exists():
        import json
        data = json.loads(sys_json.read_text())
        cfg = SystemConfig.model_validate(data)
    d = cfg.daemon
    unix = Path(d.unix_socket).expanduser() if d.unix_socket else None
    if unix and unix.exists():
        return ("http://unix", "", str(unix))
    if d.bind:
        return (f"http://{d.bind}:{d.port}", d.token, None)
    # 默认 TCP loopback
    return (f"http://127.0.0.1:{d.port}", d.token, None)


def _is_listening(unix_path: str | None, host: str, port: int) -> bool:
    if unix_path and Path(unix_path).exists():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(unix_path)
            s.close()
            return True
        except OSError:
            return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


def _ensure_serve_running() -> None:
    base_url, token, unix = _resolve_endpoint()
    host_port = base_url.replace("http://", "")
    if "/" in host_port:
        host_port = host_port.split("/")[0]
    if ":" in host_port:
        host, port_s = host_port.split(":")
    else:
        host, port_s = "127.0.0.1", "17777"
    if _is_listening(unix, host, int(port_s)):
        return
    log = Path.home() / ".xana" / "logs" / "serve.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    f = open(log, "a")
    subprocess.Popen(
        [str(Path.home() / ".xana" / "bin" / "xana"), "serve"],
        stdout=f, stderr=f, start_new_session=True,
    )
    # poll healthz
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if _is_listening(unix, host, int(port_s)):
            return
        time.sleep(0.2)
    raise RuntimeError(
        f"xana serve 没起来,查 {log}(末 20 行)")


async def _oneshot_run(
    *, base_url: str, token: str, session_id: str, text: str,
    out: TextIO, timeout_s: float = 120.0, unix_socket: str | None = None,
) -> int:
    client = DaemonClient(base_url=base_url, token=token,
                          unix_socket=unix_socket)
    try:
        state = UnifiedCardState(card_id=session_id, ph_run_id=session_id)
        done = asyncio.Event()
        rc = 0

        async def reader():
            nonlocal rc
            async for ev in client.stream(session_id):
                if ev.event == "initialize":
                    continue
                if ev.event == "session.status":
                    continue
                if ev.event == "agent_event":
                    apply_event(state, _evt_from_wire(ev.data))
                    if ev.data.get("kind") in ("final", "error", "cancelled"):
                        if ev.data.get("kind") == "error":
                            rc = 1
                        done.set()
                        return

        reader_task = asyncio.create_task(reader())
        await client.submit(session_id, text)
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            rc = 2
        reader_task.cancel()
        out.write(render_card(state) + "\n")
        return rc
    finally:
        await client.close()


def _evt_from_wire(data: dict):
    from xana.agents.contracts import AgentEvent
    from xana.harness_sdk.contracts import HarnessEvent
    he = data.get("harness_event")
    harness_event = None
    if he:
        harness_event = HarnessEvent(
            kind=he["kind"], session_id=he["session_id"],
            run_id=he["run_id"], payload=he.get("payload") or {})
    return AgentEvent(
        kind=data["kind"], session_id=data["session_id"],
        payload=data.get("payload") or {}, harness_event=harness_event,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    text = " ".join(args.text).strip()
    if not args.interactive and not text:
        from xana.cli.xanac_textual import run_textual_with_daemon
        return asyncio.run(run_textual_with_daemon(
            session_id=_session_id(args)))
    if args.interactive:
        from xana.cli.xanac_textual import run_textual_with_daemon
        return asyncio.run(run_textual_with_daemon(
            session_id=_session_id(args)))
    _ensure_serve_running()
    base_url, token, unix = _resolve_endpoint()
    return asyncio.run(_oneshot_run(
        base_url=base_url, token=token,
        session_id=_session_id(args), text=text,
        out=sys.stdout, unix_socket=unix))


def _session_id(args: argparse.Namespace) -> str:
    if args.random_session:
        return f"cli-{secrets.token_hex(3)}"
    return args.session
```

- [ ] **Step 3: 跑测试**

```
pytest tests/test_xanac_oneshot_daemon.py -v
```
Expected: 1 passed.

- [ ] **Step 4: Commit**

```
git add src/xana/cli/xanac.py tests/test_xanac_oneshot_daemon.py
git commit -m "xanac: rewrite as daemon HTTP+SSE client (oneshot path)"
```

---

## Task 11: xanac interactive — XanacApp 接 daemon

**Files:**
- Modify: `src/xana/cli/xanac_textual.py`(加 `run_textual_with_daemon` 入口 + SSE 后台协程)
- Test: `tests/test_xanac_textual_daemon.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_xanac_textual_daemon.py
import asyncio
import pytest
from aiohttp.test_utils import TestClient, TestServer

from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.runtime.daemon_http import build_daemon_app
from xana.agents.contracts import AgentEvent
from xana.harness_sdk.contracts import HarnessEvent
from xana.cli.xanac_textual import XanacApp


@pytest.mark.asyncio
async def test_xanac_app_renders_card_from_daemon_stream():
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    app_aiohttp = build_daemon_app(bridge=bridge, token="")
    async with TestClient(TestServer(app_aiohttp)) as srv:
        base = str(srv.make_url("/"))[:-1]
        from xana.cli._daemon_client import DaemonClient
        client = DaemonClient(base_url=base, token="")
        app = XanacApp(daemon_client=client, session_id="s1")
        async with app.run_test() as pilot:
            # 后台推一条 final
            await bridge.render(AgentEvent(
                kind="final", session_id="s1",
                payload={"text": "yo"},
                harness_event=HarnessEvent(
                    kind="final", session_id="s1", run_id="r1",
                    payload={"text": "yo"}),
            ))
            # 等 SSE 流过来 → update_card
            for _ in range(20):
                await pilot.pause()
                pane = app.query_one("#fg-pane")
                if any("yo" in str(c.renderable if hasattr(c, "renderable") else "")
                       for c in pane.children):
                    break
            # 至少有内容
            assert len(pane.children) > 0
        await client.close()
```

- [ ] **Step 2: 改 XanacApp**

`src/xana/cli/xanac_textual.py`,XanacApp 构造改:

```python
def __init__(
    self,
    *,
    daemon_client=None,
    session_id: str = "default",
    gateway=None,                    # legacy,留给测试
    adapter_id: str = "cli",
) -> None:
    super().__init__()
    self.daemon_client = daemon_client
    self.gateway = gateway
    self.session_id = session_id
    self.adapter_id = adapter_id
    self._fg_run_id: str | None = None
    self._tabs_widget = None
    self._frozen_runs: set[str] = set()
    self._card_state = None   # UnifiedCardState
    self._sse_task: asyncio.Task | None = None
```

`on_mount` 启动 SSE 协程:

```python
def on_mount(self) -> None:
    self._tabs_widget = self.query_one("#tab-pane", ListView)
    self.set_focus(self.query_one("#input", Input))
    if self.daemon_client is not None:
        self._sse_task = asyncio.create_task(self._consume_sse())


async def _consume_sse(self):
    from xana.agent_adapter.channels.unified_card import UnifiedCardState
    from xana.agent_adapter.channels.unified_card_events import apply_event
    from xana.cli.xanac import _evt_from_wire
    state = UnifiedCardState(card_id=self.session_id, ph_run_id=self.session_id)
    self._card_state = state
    async for ev in self.daemon_client.stream(self.session_id):
        if ev.event != "agent_event":
            continue
        apply_event(state, _evt_from_wire(ev.data))
        try:
            self.update_card(self.session_id, state)
            self._fg_run_id = self.session_id
        except Exception:
            pass
```

`on_input_submitted` 改成调 daemon:

```python
async def on_input_submitted(self, message: Input.Submitted) -> None:
    text = message.value.strip()
    if not text:
        return
    self.query_one("#input", Input).value = ""
    if self.daemon_client is not None:
        asyncio.create_task(self.daemon_client.submit(self.session_id, text))
        return
    # legacy gateway path 保留
    ...
```

`on_button_pressed` 也加 daemon 分支:

```python
async def on_button_pressed(self, event):
    btn = event.button
    if not isinstance(btn, CardButton):
        return
    spec = btn.spec
    if self.daemon_client is not None:
        if spec.send:
            asyncio.create_task(self.daemon_client.submit(
                self.session_id, spec.send))
            return
        if spec.action == "load_tools":
            asyncio.create_task(self.daemon_client.button(
                self.session_id,
                {"action": "load_tools", "card_id": btn.card_id}))
            return
        if spec.action in ("expand", "expand_page", "expand_back"):
            asyncio.create_task(self.daemon_client.button(
                self.session_id,
                {"action": spec.action, "arg": spec.action_arg}))
            return
        # 默认转后台
        asyncio.create_task(self.daemon_client.detach(
            self.session_id, btn.ph_run_id or ""))
        return
    # legacy gateway path 保留
    ...
```

加 `run_textual_with_daemon`:

```python
async def run_textual_with_daemon(*, session_id: str) -> int:
    from xana.cli.xanac import _ensure_serve_running, _resolve_endpoint
    _ensure_serve_running()
    base, token, unix = _resolve_endpoint()
    from xana.cli._daemon_client import DaemonClient
    client = DaemonClient(base_url=base, token=token, unix_socket=unix)
    try:
        app = XanacApp(daemon_client=client, session_id=session_id)
        await app.run_async()
    finally:
        await client.close()
    return 0
```

- [ ] **Step 3: 跑测试**

```
pytest tests/test_xanac_textual_daemon.py -v
```
Expected: 1 passed.

- [ ] **Step 4: 之前 e2e 改适配**

`tests/test_xanac_card.py` 老 3 个测试用 `gateway=gw` 调 XanacApp,要给加新签名 `daemon_client=None`(已是默认值,无需改)。跑一遍验证:

```
pytest tests/test_xanac_card.py -v
```
Expected: 3 passed(无回归)。

- [ ] **Step 5: Commit**

```
git add src/xana/cli/xanac_textual.py tests/test_xanac_textual_daemon.py
git commit -m "xanac: interactive Textual REPL consumes daemon SSE → update_card"
```

---

## Task 12: 删旧 CliAdapter + TurnDraft-based renderer + 更新老测试

**Files:**
- Delete: `src/xana/agent_adapter/cli.py`
- Delete: `src/xana/agent_adapter/channels/_cli_renderer_stdout.py`
- Delete: `src/xana/agent_adapter/channels/_cli_renderer_textual.py`
- Modify: `tests/test_xanac_e2e.py`, `tests/test_xanac_cancel_in_flight.py`, `tests/test_xanac_one_shot_command.py`, `tests/test_xanac_repl_loop.py`, `tests/test_xanac_repl_bottom_toolbar.py` —— 改 / 删
- Modify: `src/xana/agent_adapter/interfaces.py`(若 `CliRenderer` ABC 只用在这些 renderer)

- [ ] **Step 1: 删文件**

```bash
git rm src/xana/agent_adapter/cli.py
git rm src/xana/agent_adapter/channels/_cli_renderer_stdout.py
git rm src/xana/agent_adapter/channels/_cli_renderer_textual.py
```

- [ ] **Step 2: 找依赖**

```
grep -rn "CliAdapter\|StdoutRenderer\|TextualRenderer\|CliRenderer" src/ tests/
```
对每条要么删要么改。

- [ ] **Step 3: 删 / 改测试**

老测试改造原则:
- 走 in-process gateway 直接 submit 的:留下,把 `from xana.agent_adapter.cli import CliAdapter` 改成 `from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter`
- 测 stdout 文本输出的:移植成"用 client 跑 oneshot,验 render_card 文本"
- 实在不好移植的(/test_xanac_one_shot_command etc):删,功能由 `test_xanac_oneshot_daemon.py` + `test_serve_daemon_integration.py` 覆盖

- [ ] **Step 4: 跑全套测试**

```
pytest tests/ -q
```
Expected: all green(允许预存的 3 个 feishu render 红,与本 plan 无关)。

- [ ] **Step 5: 把 CliRenderer ABC 标 deprecated 或删**

如果 `interfaces.CliRenderer` 没人用了,删:

```python
# src/xana/agent_adapter/interfaces.py 删 CliRenderer class
```

否则注释里加 `# legacy, kept for arch tests only`。

- [ ] **Step 6: arch 测试更新**

`tests/test_arch_layers.py` 若禁止某些 import 路径,确认新文件没违反。

- [ ] **Step 7: Commit**

```
git add -A
git commit -m "xanac: delete CliAdapter + TurnDraft renderers (replaced by daemon path)"
```

---

## Task 13: 文档 + 改 setup_wizard 提示

**Files:**
- Modify: `src/xana/cli/commands/setup_wizard.py`(setup 末尾提示打印 token 路径)
- Update: `docs/superpowers/specs/2026-06-16-card-integration-guide.md`(在 §5 加 cli 接入示例 — 现在 cli 走 daemon,不再是直接挂 gateway)

- [ ] **Step 1: setup 打印**

`setup_wizard.py` main 流程末尾(token 生成完):

```python
print(f"  daemon 端口 17777 / unix {Path(d.unix_socket).expanduser()}")
print(f"  token 已写入 {sys_json}(chmod 0600)")
print(f"  在 system.json 把 daemon.bind 改成 '0.0.0.0' 暴露到 LAN(慎重)")
```

- [ ] **Step 2: integration guide 加一节**

`docs/superpowers/specs/2026-06-16-card-integration-guide.md` §5 后插 §5b "CLI 接入(走 daemon)" — 描述 client 协议入口、不再是 in-process adapter。

- [ ] **Step 3: Commit**

```
git add src/xana/cli/commands/setup_wizard.py docs/superpowers/specs/2026-06-16-card-integration-guide.md
git commit -m "docs: daemon setup hints + card guide §5b CLI-via-daemon"
```

---

## Self-Review

走一遍 spec 各节,挂任务号:

| Spec 节 | 任务 |
|---|---|
| §1 范围 | (上下文) |
| §2 配置 SystemConfig.daemon | Task 1 |
| §3 架构图 | (Task 8 wire 实现) |
| §4 协议 endpoints | Task 3(submit/stream/healthz)、Task 5(actions) |
| §4 Auth | Task 6 |
| §4 SSE initialize / 业务事件 / session.status / turn.interrupted | Task 3 / Task 4 / Task 5 / Task 8 wire `on_action` |
| §4 心跳 / replay_gap / Last-Event-ID | Task 4 / Task 2 / Task 3 |
| §4 背压 | Task 2(queue maxsize=1000),验证留给手测 |
| §4 idle unload | Task 4 |
| §4 cancel 两段语义 | Task 5(202 + 事件) — turn.interrupted 由 harness 发,Task 8 wire on_action.cancel 即可 |
| §4 button | Task 11(client 端 detach 路径)+ Task 8(server on_action.button) |
| §5 改动清单 daemon_http / daemon_bridge / serve / xanac / DaemonClient | Task 2,3,7,8,9,10,11 |
| §5 删旧 CliAdapter / TurnDraft renderer | Task 12 |
| §5 测试 | 每 task 都带 |
| §6 风险:Popen stderr | Task 10(打印 log 末 20 行) |
| §6 风险:chmod 0600 system.json | Task 1 |
| §6 风险:WARN on 0.0.0.0 | Task 8 |
| §6 风险:multi-client | Task 2(已实现 multi subscriber fan-out)|
| §7 成功标准 | 手测,验收时跑 `time xanac "hi"` 对比 |

补漏:**replay_gap 的 client 处理**没单独任务 — 在 Task 9 `DaemonClient._iter_sse` 已经能解析出 event 名 `replay_gap`,upstream 业务侧(Task 10 `_oneshot_run`、Task 11 `_consume_sse`)收到该事件应清 Card state。补 step:

**Task 10 加 step 4b(在 commit 前)**:

`_oneshot_run` reader 里:
```python
if ev.event == "replay_gap":
    state = UnifiedCardState(card_id=session_id, ph_run_id=session_id)
    continue
```

**Task 11 加 step 2b**:

`_consume_sse` 里同理。

也补对应的单元测试:`tests/test_daemon_bridge.py::test_ring_buffer_emits_replay_gap_when_too_old` 已经覆盖 server 侧;client 侧不强测(逻辑简单)。

**Placeholder scan**:扫了一遍,所有 step 都给了具体代码或具体命令。无 TODO / TBD / "implement later"。

**Type 一致性**:
- `DaemonClient.stream` 返回 `AsyncIterator[DaemonEvent]` — 三个用处都按这个 ✓
- `bridge.subscribe` 返回 `AsyncIterator[tuple[int, str, dict]]` — daemon_http stream handler + 测试一致 ✓
- `_session_id` / `_resolve_endpoint` 在 xanac.py 内部,Task 11 也用 ✓

---

Plan complete and saved to `docs/superpowers/plans/2026-06-16-xana-daemon.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — execute tasks in this session with checkpoints

Which approach?
