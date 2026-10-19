"""记忆召回的选择器:一次不带工具、关闭思考的小模型调用,从候选记忆里挑出确定有用的(最多 5 条)。

提示词译自 Claude Code memdir/findRelevantMemories.ts 的 SELECT_MEMORIES_SYSTEM_PROMPT
(docs/design/memory-recall-design.md §3.3)。boot 组装后注入 ProjectMemoryInjector。
"""
from __future__ import annotations

import json
import re

from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.llm_driver.interfaces import ModelRegistry
from xana.memory.contracts import MemoryRecord

SELECT_MEMORIES_PROMPT = (
    "你在为 xana 挑选记忆,这些记忆会在它处理用户的这条消息时提供帮助。你会拿到用户的消息,以及可用记忆的"
    "文件名、类型和描述。\n\n"
    "返回对处理这条消息**明确有用**的记忆文件名(最多 5 个)。只根据名称和描述挑你确定有帮助的:\n"
    "- 不确定是否有用就不要选。要挑剔、有取舍。\n"
    "- 列表里没有明确有用的记忆时,返回空列表即可。\n\n"
    '只输出 JSON,不要其他文字:{"selected_memories": ["文件名", ...]}'
)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class ModelMemorySelector:
    def __init__(self, *, model_registry: ModelRegistry, provider: str, model: str) -> None:
        self._registry = model_registry
        self._provider = provider
        self._model = model

    async def __call__(self, query: str, candidates: tuple[MemoryRecord, ...]) -> tuple[str, ...]:
        manifest = "\n".join(f"- {r.slug}.md [{r.type}]:{r.name} — {r.description}" for r in candidates)
        client = await self._registry.get(self._provider)
        request = ModelRequest(
            provider=self._provider, model=self._model,
            messages=(ModelMessage(role="system", content=SELECT_MEMORIES_PROMPT),
                      ModelMessage(role="user", content=f"用户的消息:{query}\n\n可用的记忆:\n{manifest}")),
            metadata={"thinking": "disabled"})
        parts: list[str] = []
        async for event in client.stream(request):
            if event.kind == "text_delta" and event.payload.get("channel") != "thinking":
                parts.append(event.payload.get("text", ""))
            elif event.kind == "error" and event.error is not None:
                raise RuntimeError(event.error.message)
        return parse_selection("".join(parts), {r.slug for r in candidates})


def parse_selection(text: str, valid: set[str]) -> tuple[str, ...]:
    """解析 {"selected_memories": [...]};文件名可带或不带 .md,不在候选里的丢弃。解析不了抛 ValueError。"""
    match = _JSON_RE.search(text)
    if match is None:
        raise ValueError(f"选择结果不是 JSON:{text[:200]!r}")
    names = json.loads(match.group(0)).get("selected_memories") or []
    out = []
    for name in names:
        slug = str(name).strip().removeprefix("memory/").removesuffix(".md")
        if slug in valid and slug not in out:
            out.append(slug)
    return tuple(out)
