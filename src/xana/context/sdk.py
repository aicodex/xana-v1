"""ContextSDK facade skeleton (§3).

Full implementation lands in P3-B. This stub demonstrates that
``_internal`` import works from inside ``xana.context.sdk`` (the guard
in ``_internal.py`` allows it) and exposes the serialization entry
points that prepend the harness-provided system content.
"""
from __future__ import annotations

from xana.context import _internal as _ctx_internal  # noqa: F401 — triggers + smoke-tests the private-module guard
from xana.context.message import Message
del _ctx_internal  # release name; full sdk impl in P3-B uses fields directly


class ContextSDK:
    """§3 facade — full impl in next task."""

    def __init__(self) -> None:
        pass

    def to_anthropic(
        self, messages: list[Message], *, system_content: str
    ) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system_content}]
        for m in messages:
            out.extend(m.to_anthropic())
        return out

    def to_openai(
        self, messages: list[Message], *, system_content: str
    ) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system_content}]
        for m in messages:
            out.extend(m.to_openai())
        return out
