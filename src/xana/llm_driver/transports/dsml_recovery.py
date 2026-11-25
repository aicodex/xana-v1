"""Recover DSML tool calls leaked into the text channel by DeepSeek-V4's
Anthropic endpoint.

DeepSeek-V4's native tool-call format is DSML. The /anthropic endpoint
should convert DSML → Anthropic tool_use blocks server-side. For client
function tools the conversion is inconsistent — the raw DSML sometimes
passes through into a text block instead (~50% rate observed). This
module finishes the conversion the endpoint skipped, so the agent loop
can run the call.

Markers: U+FF5C full-width pipe, double form (endpoint-escaped) or
single form (model-card form). Both are accepted defensively.

A complete fenced block (BOTH open AND close present) is required to
treat anything as a leak — guards against false positives on prose.

Parameter value semantics: ``string="true"`` → raw string; otherwise
the inner text is JSON-parsed (numbers, bools, lists, objects). Malformed
JSON degrades to raw string rather than raising.
"""
from __future__ import annotations

import json
import re
from typing import Any

# U+FF5C, one or two pipes (endpoint = 2 / model card = 1).
_P = "｜{1,2}"
_DSML_OPEN = rf"<{_P}DSML{_P}"
_DSML_CLOSE = rf"</{_P}DSML{_P}"

# Open/close marker pairs for the streaming scrubber (StreamingContextScrubber
# does literal substring matching, so the variable-pipe regex can't be used —
# enumerate both the endpoint's double-pipe and the model card's single-pipe).
DSML_FENCE_TAGS: tuple[tuple[str, str], ...] = (
    ("<｜｜DSML｜｜tool_calls>", "</｜｜DSML｜｜tool_calls>"),
    ("<｜DSML｜tool_calls>", "</｜DSML｜tool_calls>"),
)

_TOOL_CALLS_RE = re.compile(
    rf"{_DSML_OPEN}tool_calls>(.*?){_DSML_CLOSE}tool_calls>",
    re.DOTALL,
)
_INVOKE_RE = re.compile(
    rf'{_DSML_OPEN}invoke\s+name="(?P<name>[^"]*)"\s*>(?P<body>.*?){_DSML_CLOSE}invoke>',
    re.DOTALL,
)
_PARAM_RE = re.compile(
    rf'{_DSML_OPEN}parameter\s+name="(?P<name>[^"]*)"'
    rf'(?:\s+string="(?P<string>true|false)")?\s*>'
    rf"(?P<value>.*?){_DSML_CLOSE}parameter>",
    re.DOTALL,
)


def contains_dsml_tool_calls(text: str | None) -> bool:
    return bool(text) and bool(_TOOL_CALLS_RE.search(text or ""))


def parse_dsml_tool_calls(text: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Extract DSML tool calls from text.

    Returns (cleaned_text, calls). Each call: ``{"name", "input"}`` (the
    caller assigns ids). cleaned_text has every tool_calls block removed
    and surrounding whitespace tidied. No DSML → (text, []).
    """
    if not text or not _TOOL_CALLS_RE.search(text):
        return (text or ""), []

    calls: list[dict[str, Any]] = []
    for block in _TOOL_CALLS_RE.finditer(text):
        for inv in _INVOKE_RE.finditer(block.group(1)):
            calls.append(_parse_invoke(inv.group("name"), inv.group("body")))

    cleaned = _TOOL_CALLS_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, calls


def _parse_invoke(name: str, body: str) -> dict[str, Any]:
    input_: dict[str, Any] = {}
    for m in _PARAM_RE.finditer(body):
        input_[m.group("name")] = _coerce_value(m.group("value"), m.group("string"))
    return {"name": name, "input": input_}


def _coerce_value(raw: str, is_string_attr: str | None) -> Any:
    if is_string_attr == "true":
        return raw
    try:
        return json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        return raw
