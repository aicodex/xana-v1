"""SDK 缓冲里的消息类型(§1.9)。

工具调用身份放在 ``_internal`` 里,不混入模型可见文本。
发给模型的形态由 harness 生成:SDK 缓冲 → memory._message_bridge → 拼提示词
→ provider;这里只保留模型可见文本的唯一定义(CompactSummary.render)。
"""
from __future__ import annotations

from dataclasses import dataclass
from xana.multimodal.contracts import MediaInput
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xana.context._internal import _ToolCallInternal


class Message:
    """SDK 缓冲中一条消息的基类(类型标记)。"""


@dataclass(frozen=True)
class UserMessage(Message):
    text: str
    media: tuple[MediaInput, ...] = ()
    input_id: str = ""


@dataclass(frozen=True)
class AssistantMessage(Message):
    text: str
    tool_calls: tuple["_ToolCallInternal", ...] = ()
    # 模型本轮的思考文本。DeepSeek / Kimi 要求原样回传(保留思考),
    # 必须随历史穿过 SDK(provider 各自决定怎么回传)。
    reasoning: str = ""


@dataclass(frozen=True)
class ToolResultMessage(Message):
    tool_use_id: str
    content: str
    ok: bool
    # harness 簿记:工具名(compact 转录用)、tool_search 揭示的工具(跨 run 保持已加载)。
    # 不进 prompt。
    tool_name: str = ""
    revealed_tools: tuple[str, ...] = ()
    media: tuple[MediaInput, ...] = ()








@dataclass(frozen=True)
class CompactSummary(Message):
    summary: str
    run_id: str
    transcript_path: str = ""
    # 压缩前已揭示的按需工具:压缩后工具结果没了,靠它保住揭示状态(CC preCompactDiscoveredTools)
    revealed_tools: tuple[str, ...] = ()

    # Verbatim from §1.9 — single source for model-visible + card body.
    POST_COMPACT_TEMPLATE = (
        "此会话正在从上一次因上下文不足而中断的对话中继续。"
        "以下摘要涵盖了对话的早期部分。\n\n"
        "摘要：\n{summary}\n\n"
        "如果你需要压缩前的具体细节(如确切的代码片段、错误信息或你生成的内容),"
        "请阅读完整转录记录:{transcript}\n"
        "继续对话,无需向用户提出任何进一步的问题。直接继续——不要确认摘要,"
        "不要复述正在发生的事情,不要以\"我将继续\"或类似语句开头。"
        "如同中断从未发生一样继续上一个任务。"
    )

    @classmethod
    def render(cls, summary: str, transcript_path: str = "") -> str:
        """Single-source renderer — same string for model prompt + card body.

        Used by both CompactSummary._render() (model side, via
        memory._message_bridge) and the UI side (commands._cmd_compact
        fires this rendered text in the compact_finalized progress event,
        so the card panel body matches what the model will see)."""
        return cls.POST_COMPACT_TEMPLATE.format(
            summary=summary, transcript=transcript_path,
        )

    def _render(self) -> str:
        return self.POST_COMPACT_TEMPLATE.format(
            summary=self.summary,
            transcript=self.transcript_path,
        )


@dataclass(frozen=True)
class ContextNote(Message):
    """harness 追加进历史的上下文消息(记忆召回、技能列表、按需工具、提醒等)。

    会话内只追加、不改写,保证请求前缀逐字节稳定;模型看到的是一条 user 角色的
    ``<system-reminder>`` 文本。``key`` / ``paths`` / ``size`` 只供 harness 去重与
    计量,不进 prompt。
    """
    kind: str
    text: str
    key: str = ""
    paths: tuple[str, ...] = ()
    size: int = 0
    input_id: str = ""

    @staticmethod
    def wrap(text: str) -> str:
        return f"<system-reminder>\n{text}\n</system-reminder>"

    def to_metadata(self) -> dict:
        """当前 MemoryMessage 形态的 metadata(content 为 ``wrap(text)``)。"""
        return {
            "note_kind": self.kind, "note_text": self.text, "note_key": self.key,
            "note_paths": list(self.paths), "note_size": self.size,
            **({"input_id": self.input_id} if self.input_id else {}),
        }

    @classmethod
    def from_metadata(cls, md: dict) -> "ContextNote":
        return cls(
            kind=str(md["note_kind"]), text=str(md.get("note_text", "")),
            key=str(md.get("note_key", "")), paths=tuple(md.get("note_paths", ())),
            size=int(md.get("note_size", 0)),
            input_id=str(md.get("input_id", "")),
        )
