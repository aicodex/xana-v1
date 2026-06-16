"""Terminal (string) renderer for UnifiedCardState.

This is the oneshot-friendly view used by xanac when it prints once
after a turn reaches a terminal state. It returns a plain-text string;
no chrome, no colours, no widgets — just the sections in their natural
order.

For the Textual interactive view, see the widget-based renderer used by
XanacApp (kept under a different module name to avoid clashing).
"""
from __future__ import annotations

from xana.agent_adapter.channels.unified_card import (
    AnswerSection,
    CompactSection,
    UnifiedCardState,
)


_BUDGET_THINKING = 4000
_BUDGET_TOOL_BODY = 2000
_BUDGET_ANSWER = 12000
_VISIBLE_TOOLS = 3


def _trunc(s: str, cap: int, suffix: str = "\n…(已折叠)") -> str:
    if len(s) <= cap:
        return s
    return s[:cap].rstrip() + suffix


def _render_compact(sec: CompactSection) -> str:
    if sec.state == "completed":
        head = f"[compact] 完成压缩 · 共 {sec.compacted_n} 条"
    elif sec.state == "cancelled":
        head = "[compact] 压缩已取消"
    else:
        head = "[compact] 压缩中"
    body = (sec.summary_text or "").rstrip()
    if body:
        return f"{head}\n{body}"
    return head


def _render_answer(sec: AnswerSection) -> str:
    parts: list[str] = []
    if sec.thinking_text.strip():
        body = _trunc(sec.thinking_text, _BUDGET_THINKING).rstrip()
        parts.append(f"❋ 思考过程\n{body}")

    visible_n = min(_VISIBLE_TOOLS, len(sec.tools))
    for t in sec.tools[:visible_n]:
        body = _trunc(t.body_md or "", _BUDGET_TOOL_BODY).rstrip()
        if body:
            parts.append(f"{t.header}\n{body}")
        else:
            parts.append(t.header)
    hidden = (len(sec.tools) - visible_n) + sec.tools_pending
    if hidden > 0:
        parts.append(f"(+{hidden} 工具调用未展开)")

    if sec.state == "cancelled":
        title = "✦ 回答已取消"
    else:
        title = "✦ 回答" if sec.answer_text else "✦ 回答中"
    body = _trunc(sec.answer_text or "", _BUDGET_ANSWER).rstrip()
    if body:
        parts.append(f"{title}\n{body}")
    else:
        parts.append(title)
    return "\n\n".join(parts)


def render(state: UnifiedCardState) -> str:
    """UnifiedCardState → plain-text string (terminal-friendly)."""
    parts: list[str] = []
    if state.compact is not None:
        parts.append(_render_compact(state.compact))
    if state.answer is not None:
        parts.append(_render_answer(state.answer))
    if state.menu is not None and state.menu.body_md:
        parts.append(state.menu.body_md.rstrip())
    footer_text = state.footer.render()
    if footer_text:
        parts.append(footer_text)
    return "\n\n".join(p for p in parts if p)
