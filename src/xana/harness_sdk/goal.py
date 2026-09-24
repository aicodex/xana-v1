"""目标模式:每轮结束由独立评估器判断目标条件是否满足(照 Claude Code 2.1.281 /goal)。

CC 的 /goal 是会话级 prompt 型 Stop hook:单独一次模型调用(关闭思考、不给工具)读对话
记录,返回 ``{"ok": ...}``;不满足则拦下本轮结束继续推进。提示词为 CC 原文的中文版。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Sequence

from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.llm_driver.interfaces import ModelClient
from xana.memory.contracts import MemoryMessage

REPORT_GOAL_TOOL = "report_goal"
GOAL_STATUSES = ("done", "blocked", "failed")
EVALUATOR_TIMEOUT_S = 30.0
TRANSCRIPT_BUDGET_CHARS = 40_000

GOAL_ACTIVATED = (
    "目标模式已开启,条件为「{condition}」。简短确认目标,然后立刻开始(或继续)朝它推进。"
    "把条件本身当作指令,不要停下来问该做什么。条件成立之前,每轮结束都会被拦下继续。"
    "如果确认目标无法达成或被外部原因阻断,调用 report_goal 说明原因。"
)

_EVALUATOR_SYSTEM = (
    "你在评估一个停止条件 hook。仔细阅读对话记录,然后判断用户给出的条件是否已经满足。"
    "你的回复必须是以下形式之一的 JSON 对象:\n"
    '- {"ok": true, "reason": "<引用对话记录中满足条件的证据>"}\n'
    '- {"ok": false, "reason": "<引用缺少什么或什么阻碍了条件>"}\n'
    '- {"ok": false, "impossible": true, "reason": "<解释为什么这个条件永远无法满足>"}\n'
    '始终包含 "reason" 字段,尽可能引用对话记录中的具体文字。如果对话记录中没有清楚的证据表明条件已满足,'
    '返回 {"ok": false, "reason": "对话记录中证据不足"}。只有当条件在本会话中确实无法达成时才使用 '
    '{"ok": false, "impossible": true}——例如:条件自相矛盾、依赖不可用的资源或能力,或者助手已经明确'
    "尝试、用尽了合理的方法并表示无法完成。做这个判断时要运用你自己的判断——助手声称目标不可能只是证据,"
    "不是证明;要独立确认条件确实无法达成,而不是照搬助手的自我评估。不要仅仅因为目标尚未达成或进展缓慢"
    '就使用它。拿不准时,返回不带 "impossible" 的 {"ok": false}。'
)
_EVALUATOR_QUESTION = "根据上面的对话记录,下面这个停止条件是否已经满足?只依据对话记录中的证据回答。条件:{condition}"
_OMITTED = (
    "[前面 {n} 条消息已省略。请依据下面最近的对话记录评估条件;如果所需证据可能在省略的部分里,"
    '返回 {{"ok": false, "reason": "对话记录中证据不足"}}。]'
)


@dataclass(frozen=True)
class GoalVerdict:
    ok: bool
    reason: str
    impossible: bool = False


def goal_continue_text(reason: str) -> str:
    return f"目标条件尚未满足:{reason}\n继续朝目标推进。"


def _transcript(history: Sequence[MemoryMessage]) -> str:
    lines: list[str] = []
    for m in history:
        if m.metadata.get("note_kind") in ("goal", "stop_hook", "continuation"):
            continue
        text = m.content
        if m.role == "assistant" and m.metadata.get("tool_calls"):
            calls = ", ".join(f"{tc.get('tool_name')}({json.dumps(tc.get('input') or {}, ensure_ascii=False)})"
                              for tc in m.metadata["tool_calls"])
            text = f"{text}\n[调用工具] {calls}".strip()
        lines.append(f"[{m.role}] {text}")
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > TRANSCRIPT_BUDGET_CHARS and kept:
            break
        kept.append(line)
        used += len(line)
    kept.reverse()
    omitted = len(lines) - len(kept)
    head = _OMITTED.format(n=omitted) + "\n\n" if omitted else ""
    return head + "\n\n".join(kept)


def _parse(text: str) -> GoalVerdict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "ok" in data:
            return GoalVerdict(ok=bool(data["ok"]), reason=str(data.get("reason", "")),
                               impossible=bool(data.get("impossible", False)))
    return GoalVerdict(ok=False, reason=f"评估器输出无法解析:{text[:200]}")


async def evaluate_goal(
    client: ModelClient, *, provider: str, model: str, condition: str,
    history: Sequence[MemoryMessage],
) -> GoalVerdict:
    """一次独立的模型调用判断目标是否达成。超时或出错抛出,由调用方决定如何处理。"""
    request = ModelRequest(
        provider=provider, model=model,
        messages=(
            ModelMessage(role="system", content=_EVALUATOR_SYSTEM),
            ModelMessage(role="user", content=_transcript(history) + "\n\n"
                         + _EVALUATOR_QUESTION.format(condition=condition)),
        ),
        metadata={"thinking": "disabled"},
    )

    async def _collect() -> str:
        parts: list[str] = []
        async for event in client.stream(request):
            if event.kind == "text_delta" and event.payload.get("channel") != "thinking":
                parts.append(event.payload.get("text", ""))
            elif event.kind == "error" and event.error is not None:
                raise RuntimeError(event.error.message)
        return "".join(parts)

    return _parse(await asyncio.wait_for(_collect(), timeout=EVALUATOR_TIMEOUT_S))
