"""Shared tool-title helpers for xana channel renderers.

Moved verbatim from cli.py so Telegram/Feishu/xanac can all share the same
● Tool(preview) format without duplicating logic.
"""
from __future__ import annotations

from typing import Any

TOOL_GLYPH = "●"
_PREVIEW_LIMIT = 80
_RESULT_PREVIEW_LIMIT = 120
_ELLIPSIS = "…"


def truncate(s: str, limit: int) -> str:
    collapsed = " ".join(s.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)] + _ELLIPSIS


def bash_comment_label(command: str) -> str | None:
    """If the first line of a bash command is a `# comment` (not a shebang),
    return its text. Mirrors cc's tools/BashTool/commentLabel.ts:8-13."""
    first_line = command.split("\n", 1)[0].strip()
    if not first_line.startswith("#") or first_line.startswith("#!"):
        return None
    label = first_line.lstrip("#").strip()
    return label or None


def first_line_preview(text: str) -> str:
    """First non-blank stripped line of `text`, collapsed + truncated."""
    if not text:
        return ""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            return truncate(stripped, _RESULT_PREVIEW_LIMIT)
    return ""


def tool_preview(tool_input: dict[str, Any], tool_name: str) -> str:
    """Pick the most informative short label for a tool call.

    Priority order mirrors cc's AssistantToolUseMessage:
      1. model-supplied input["description"];
      2. Bash `# label` comment on the command's first line;
      3. tool-specific main field (file_path / pattern / query / command);
      4. flat kv preview as a last resort.
    """
    desc = tool_input.get("description")
    if isinstance(desc, str) and desc.strip():
        return truncate(desc.strip(), _PREVIEW_LIMIT)

    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        if command:
            label = bash_comment_label(command)
            if label:
                return truncate(label, _PREVIEW_LIMIT)
            return truncate(command, _PREVIEW_LIMIT)

    main_field_by_tool: dict[str, tuple[str, ...]] = {
        "Read": ("file_path", "path"),
        "Write": ("file_path", "path"),
        "Edit": ("file_path", "path"),
        "Glob": ("pattern",),
        "Grep": ("pattern",),
        "memory_search": ("query",),
    }
    for field in main_field_by_tool.get(tool_name, ()):
        value = tool_input.get(field)
        if isinstance(value, str) and value:
            return truncate(value, _PREVIEW_LIMIT)

    if not tool_input:
        return ""
    if len(tool_input) == 1:
        only = next(iter(tool_input.values()))
        return truncate(str(only), _PREVIEW_LIMIT)
    return truncate(" ".join(f"{k}={v}" for k, v in tool_input.items()), _PREVIEW_LIMIT)


def tool_header(
    tool_name: str,
    tool_input: dict,
    *,
    ok: bool = True,
    running: bool = False,
    elapsed_ms: int | None = None,
) -> str:
    """xanac 风格工具标题: `● Tool(preview)`,失败/运行中/耗时追加后缀。
    tg/feishu 折叠块标题与 xanac CLI 共用此格式。"""
    header = TOOL_GLYPH + " " + tool_name
    preview = tool_preview(tool_input or {}, tool_name)
    if preview:
        header += f"({preview})"
    if not ok:
        header += " — FAILED"
    elif running:
        header += " — ⏳ 执行中"
    elif elapsed_ms is not None and elapsed_ms >= 0:
        header += f" — {int(elapsed_ms)}ms"
    return header
