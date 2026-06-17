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
            transport=transport,
            timeout=httpx.Timeout(connect=2.0, read=None,
                                  write=10.0, pool=10.0),
        )
        self._last_seq: dict[str, int] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def stream(self, sid: str) -> AsyncIterator[DaemonEvent]:
        """SSE subscribe with auto-reconnect + Last-Event-ID resume."""
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
        body: dict = {"text": text}
        if metadata:
            body["metadata"] = metadata
        r = await self._client.post(f"/sessions/{sid}/submit", json=body)
        r.raise_for_status()

    async def resolve(self, adapter: str = "cli", thread: str = "") -> str | None:
        """Ask daemon for the current sid bound to (adapter, thread).

        Returns None if the daemon's SessionResolver isn't wired (legacy
        deployment). Callers should fall back to a client-side sid.
        """
        try:
            r = await self._client.get(
                "/resolve", params={"adapter": adapter, "thread": thread},
            )
            r.raise_for_status()
            sid = str(r.json().get("session_id") or "")
            return sid or None
        except Exception:
            return None

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
    """Parse SSE id/event/data frames. Comment lines (':...') ignored."""
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
            continue
        if line.startswith("id:"):
            try:
                cur_id = int(line[3:].strip())
            except ValueError:
                cur_id = 0
        elif line.startswith("event:"):
            cur_event = line[6:].strip()
        elif line.startswith("data:"):
            cur_data.append(line[5:].lstrip())
