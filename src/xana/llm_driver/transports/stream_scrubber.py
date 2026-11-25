"""Streaming context scrubber.

Faithful port of yuxu ``src/yuxu/_stream_scrubber.py`` (class
``StreamingContextScrubber``, itself a port of Hermes
``agent/memory_manager.py:62-170``). Strips fenced spans from streamed
LLM output before it reaches the user.

In xana this is used by ``anthropic_deepseek._parse_sse_stream`` with
``dsml_recovery.DSML_FENCE_TAGS`` so leaked raw DSML tool-call markup
never reaches the user-facing text channel — the raw text still feeds
the post-stream DSML recovery scan, which re-emits the calls as proper
``tool_call`` events.

The state machine survives chunk boundaries where an open tag arrives
in one SSE delta and the close tag several deltas later. A one-shot
regex can't match across chunks because the buffer at any one moment
doesn't contain both tags.
"""
from __future__ import annotations

from typing import Sequence


class StreamingContextScrubber:
    """Stateful scrubber for streamed text containing split fenced spans.

    Usage::

        scrubber = StreamingContextScrubber(tags)
        for delta in stream:
            visible = scrubber.feed(delta)
            if visible:
                emit(visible)
        trailing = scrubber.flush()  # end of stream
        if trailing:
            emit(trailing)

    Each scrubber instance is single-stream. Caller should construct a
    fresh one (or call ``reset()``) at the start of each LLM call.

    Multi-tag support: pass a ``tags`` sequence of ``(open, close)``
    pairs; literal substring matching, case-insensitive.
    """

    def __init__(self, tags: Sequence[tuple[str, str]]) -> None:
        if not tags:
            raise ValueError("tags must be non-empty")
        self._tags = tuple(tags)
        # Lower-cased copies for the scan loop.
        self._tags_lower = tuple((o.lower(), c.lower()) for o, c in self._tags)
        # in_span_idx == -1 ⇒ outside any span; else index into self._tags
        self._in_span_idx: int = -1
        self._buf: str = ""

    def reset(self) -> None:
        self._in_span_idx = -1
        self._buf = ""

    def feed(self, text: str) -> str:
        """Return the visible portion of ``text`` after scrubbing.

        Any trailing fragment that *could* be the start of any open or
        close tag is held back in the internal buffer and surfaced on
        the next ``feed()`` or by ``flush()``.
        """
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        buf_lower = buf.lower()
        out: list[str] = []
        pos = 0

        while pos < len(buf):
            if self._in_span_idx >= 0:
                close_tag = self._tags_lower[self._in_span_idx][1]
                idx = buf_lower.find(close_tag, pos)
                if idx == -1:
                    # Inside an unterminated span — hold back any
                    # partial close-tag suffix for the next feed; drop
                    # everything else (it's span content).
                    held = self._max_partial_suffix(buf_lower[pos:], close_tag)
                    if held:
                        self._buf = buf[len(buf) - held:]
                    return "".join(out)
                # Close found — skip span content + close tag.
                pos = idx + len(close_tag)
                self._in_span_idx = -1
                continue

            # Outside any span — find the earliest open tag.
            next_open_idx = -1
            next_open_tag_idx = -1
            for ti, (open_lower, _close_lower) in enumerate(self._tags_lower):
                cand = buf_lower.find(open_lower, pos)
                if cand == -1:
                    continue
                if next_open_idx == -1 or cand < next_open_idx:
                    next_open_idx = cand
                    next_open_tag_idx = ti
            if next_open_idx == -1:
                # No open tag in remaining buffer — hold back the
                # longest suffix that could be a partial open tag for
                # ANY known tag, emit the rest.
                tail = buf[pos:]
                tail_lower = buf_lower[pos:]
                held = 0
                for open_lower, _ in self._tags_lower:
                    candidate = self._max_partial_suffix(tail_lower, open_lower)
                    if candidate > held:
                        held = candidate
                if held:
                    out.append(tail[:-held])
                    self._buf = tail[-held:]
                else:
                    out.append(tail)
                return "".join(out)
            # Emit pre-tag content, enter span.
            if next_open_idx > pos:
                out.append(buf[pos:next_open_idx])
            open_tag_len = len(self._tags_lower[next_open_tag_idx][0])
            pos = next_open_idx + open_tag_len
            self._in_span_idx = next_open_tag_idx

        return "".join(out)

    def flush(self) -> str:
        """Emit any held-back buffer at end-of-stream.

        If we're still inside an unterminated span the remaining content
        is **discarded** — leaking partial internal markup is worse than
        a truncated answer. Otherwise the held-back partial-tag tail is
        emitted verbatim (it turned out not to be a real tag).
        """
        if self._in_span_idx >= 0:
            self._buf = ""
            self._in_span_idx = -1
            return ""
        tail = self._buf
        self._buf = ""
        return tail

    # -- helpers ----------------------------------------------------

    @staticmethod
    def _max_partial_suffix(buf_lower: str, tag_lower: str) -> int:
        """Return the longest suffix of ``buf_lower`` that is a prefix
        of ``tag_lower``. Case-insensitive — caller pre-lowercased.

        Returns ``0`` if no proper suffix could start the tag.
        """
        max_check = min(len(buf_lower), len(tag_lower) - 1)
        for i in range(max_check, 0, -1):
            if tag_lower.startswith(buf_lower[-i:]):
                return i
        return 0


__all__ = ["StreamingContextScrubber"]
