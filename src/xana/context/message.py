"""Message ABC + public subclasses (§1.9).

These classes are the ONLY way context flows into model prompts. Private
metadata (run_id / ph_id / call_id) lives in ``_internal`` dataclasses
that never appear in serialized output (I18).
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xana.context._internal import _PlaceholderInternal, _ToolCallInternal


class Message(ABC):
    @abstractmethod
    def to_anthropic(self) -> list[dict]:  # empty list = filter
        ...

    @abstractmethod
    def to_openai(self) -> list[dict]:
        ...


@dataclass(frozen=True)
class UserMessage(Message):
    text: str

    def to_anthropic(self) -> list[dict]:
        return [{"role": "user", "content": self.text}]

    def to_openai(self) -> list[dict]:
        return [{"role": "user", "content": self.text}]


@dataclass(frozen=True)
class AssistantMessage(Message):
    text: str
    tool_calls: tuple["_ToolCallInternal", ...] = ()
    # 模型本轮的思考文本。DeepSeek / Kimi 要求原样回传(保留思考),
    # 必须随历史穿过 SDK;Anthropic thinking 需签名,to_anthropic 不发。
    reasoning: str = ""

    def to_anthropic(self) -> list[dict]:
        blocks: list[dict] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for tc in self.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.call_id,
                    "name": tc.name,
                    "input": tc.arguments,
                }
            )
        return [{"role": "assistant", "content": blocks}]

    def to_openai(self) -> list[dict]:
        msg: dict = {"role": "assistant", "content": self.text}
        if self.reasoning:
            msg["reasoning_content"] = self.reasoning
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.call_id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]
        return [msg]


@dataclass(frozen=True)
class ToolResultMessage(Message):
    tool_use_id: str
    content: str
    ok: bool
    # harness 簿记:工具名(compact 转录用)、tool_search 揭示的工具(跨 run 保持已加载)。
    # 不进 prompt。
    tool_name: str = ""
    revealed_tools: tuple[str, ...] = ()

    def to_anthropic(self) -> list[dict]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": self.tool_use_id,
                        "content": self.content,
                        "is_error": not self.ok,
                    }
                ],
            }
        ]

    def to_openai(self) -> list[dict]:
        return [
            {
                "role": "tool",
                "tool_call_id": self.tool_use_id,
                "content": self.content,
            }
        ]


def regular_placeholder_text(label: str) -> str:
    """普通 PH 的模型可见文本(docs/design/session-and-card-state.md §1.3,逐字);唯一定义,
    SDK 序列化与 harness 拼提示词共用。"""
    return f"正在后台回答你的问题{label}，请勿重试此请求。"


@dataclass(frozen=True)
class RegularPlaceholder(Message):
    label: str  # public: original user question
    _internal: "_PlaceholderInternal"  # private: run_id / ph_id

    def to_anthropic(self) -> list[dict]:
        return [
            {
                "role": "assistant",
                "content": regular_placeholder_text(self.label),
            }
        ]

    def to_openai(self) -> list[dict]:
        return [
            {
                "role": "assistant",
                "content": regular_placeholder_text(self.label),
            }
        ]


@dataclass(frozen=True)
class CompactPlaceholder(Message):
    _internal: "_PlaceholderInternal"

    def to_anthropic(self) -> list[dict]:
        return []  # invisible

    def to_openai(self) -> list[dict]:
        return []  # invisible


@dataclass(frozen=True)
class CompactSummary(Message):
    summary: str
    _internal: "_PlaceholderInternal"
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
        to_anthropic/to_openai) and the UI side (commands._cmd_compact
        fires this rendered text in the compact_finalized progress event,
        so the card panel body matches what the model will see)."""
        return cls.POST_COMPACT_TEMPLATE.format(
            summary=summary, transcript=transcript_path,
        )

    def _render(self) -> str:
        return self.POST_COMPACT_TEMPLATE.format(
            summary=self.summary,
            transcript=self._internal.transcript_path,
        )

    def to_anthropic(self) -> list[dict]:
        return [{"role": "user", "content": self._render()}]

    def to_openai(self) -> list[dict]:
        return [{"role": "user", "content": self._render()}]


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

    @staticmethod
    def wrap(text: str) -> str:
        return f"<system-reminder>\n{text}\n</system-reminder>"

    def legacy_metadata(self) -> dict:
        """legacy MemoryMessage 形态的 metadata(content 为 ``wrap(text)``)。"""
        return {
            "note_kind": self.kind, "note_text": self.text, "note_key": self.key,
            "note_paths": list(self.paths), "note_size": self.size,
        }

    @classmethod
    def from_legacy_metadata(cls, md: dict) -> "ContextNote":
        return cls(
            kind=str(md["note_kind"]), text=str(md.get("note_text", "")),
            key=str(md.get("note_key", "")), paths=tuple(md.get("note_paths", ())),
            size=int(md.get("note_size", 0)),
        )

    def to_anthropic(self) -> list[dict]:
        return [{"role": "user", "content": self.wrap(self.text)}]

    def to_openai(self) -> list[dict]:
        return [{"role": "user", "content": self.wrap(self.text)}]
