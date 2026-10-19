"""记忆的后台提取:每轮结束后,用一次小模型调用把这一轮里值得长期记住、但没被记下的东西写进记忆
(docs/design/memory-dream-design.md §3.1;照 Claude Code services/extractMemories 与 Codex 记忆提取)。

- 所有带 memory 工具组的会话都跑(聊天会话与定时任务等无人值守运行),agent_run 派出的子 agent 不跑;
  这一轮里模型已经自己写过记忆就跳过(由 harness 判断)。
- 只做两种写入:新建一条记忆,或在已有记忆后追加一段;与已有记忆相近时改为追加。不改写、不删除。
- 每 every 轮提取一次(按会话计数);失败只记日志,不影响对话。
"""
from __future__ import annotations

import json
import logging
import re

from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.llm_driver.interfaces import ModelRegistry
from xana.memory.contracts import MEMORY_TYPES, ExtractionRequest, MemoryMessage
from xana.memory.store import ProjectMemoryStore

_logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_CHARS = 12000
TOOL_RESULT_CHARS = 300
MAX_WRITES = 3
SIMILAR_THRESHOLD = 0.6
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

EXTRACT_PROMPT = """\
你是记忆提取助手。下面是一轮对话(或一次无人值守运行)的记录,以及项目里已有记忆的索引。找出其中**以后的会话会因此做得更好**、而且还没被记下的东西,写成记忆。

什么时候值得记(四类):
- user:了解到用户是谁、偏好、专长、关注的方向。
- feedback:用户纠正了做法("不是这样""别这么做""你错在……"),或认可了一个不显而易见的做法("对,就这样",或接受了一个不寻常的选择没有反对)。纠正容易注意到,认可更安静,要留意。写清规则本身、**原因**(用户给的理由,常常是一次教训)和**怎么用**(什么场合适用)。
- project:进行中的工作、目标、约束、决定。相对日期改成绝对日期。
- reference:外部资源在哪、做什么用(数据目录、文档、看板)。
{mode_hint}
不要记:能从文件或工具直接查到的东西;只对这一轮有用的临时信息;常识;实时数据(行情、价格);对话里的过程叙述。**宁可不记,也不要乱记**——没有值得记的就返回空列表。

已有记忆与这条相近时,用 append 追加到那条记忆后面,不要新建重复的。

只输出 JSON,不要其他文字:
{{"memories": [
  {{"action": "create", "path": "小写英文文件名", "name": "简短名称", "description": "一句话,写清它在什么情况下有用", "type": "user|feedback|project|reference", "topic": "可选主题,如 估值", "content": "正文"}},
  {{"action": "append", "path": "已有记忆的文件名", "content": "追加的一段"}}
]}}"""

_INTERACTIVE_HINT = "这是用户在线的聊天会话:重点看用户说的话——纠正、偏好、规则和他给出的理由。\n"
_UNATTENDED_HINT = ("这是无人值守的运行(定时任务或长期运行的 agent):重点看执行中得到的经验——哪种做法有效、"
                    "踩过什么坑以及怎么避开、环境与数据源的事实。\n")


class ModelMemoryExtractor:
    def __init__(self, *, model_registry: ModelRegistry, provider: str, model: str,
                 store: ProjectMemoryStore, every: int = 1) -> None:
        self._registry = model_registry
        self._provider = provider
        self._model = model
        self._store = store
        self._every = max(1, every)
        self._counts: dict[str, int] = {}

    async def __call__(self, request: ExtractionRequest) -> None:
        count = self._counts.get(request.session_id, 0) + 1
        self._counts[request.session_id] = count
        if count % self._every:
            return
        transcript = render_transcript(request.messages)
        if not transcript.strip():
            return
        index = self._store.index_text() or "(还没有记忆)"
        system = EXTRACT_PROMPT.format(mode_hint=_INTERACTIVE_HINT if request.interactive else _UNATTENDED_HINT)
        user = f"已有记忆的索引:\n{index}\n\n本轮记录:\n{transcript}"
        try:
            ops = parse_extraction(await self._complete(system, user))
        except Exception as exc:
            _logger.warning("记忆提取失败(会话 %s):%r", request.session_id, exc)
            return
        for path in self.apply(ops):
            _logger.info("记忆提取:会话 %s 写入 %s", request.session_id, path)

    async def _complete(self, system: str, user: str) -> str:
        client = await self._registry.get(self._provider)
        request = ModelRequest(provider=self._provider, model=self._model,
                               messages=(ModelMessage(role="system", content=system),
                                         ModelMessage(role="user", content=user)),
                               metadata={"thinking": "disabled"})
        parts: list[str] = []
        async for event in client.stream(request):
            if event.kind == "text_delta" and event.payload.get("channel") != "thinking":
                parts.append(event.payload.get("text", ""))
            elif event.kind == "error" and event.error is not None:
                raise RuntimeError(event.error.message)
        return "".join(parts)

    def apply(self, ops: list[dict]) -> list[str]:
        """执行提取结果:create(已存在或与已有记忆相近时改为追加)/ append。返回写入的记忆路径。"""
        written: list[str] = []
        for op in ops[:MAX_WRITES]:
            content = str(op.get("content") or "").strip()
            path = str(op.get("path") or "").strip()
            if not content or not path:
                continue
            try:
                if op.get("action") == "append":
                    if self._store.get(path) is not None:
                        written.append(self._store.append(path, content).path)
                    continue
                name, desc = str(op.get("name") or "").strip(), str(op.get("description") or "").strip()
                mtype = str(op.get("type") or "project")
                if not name or not desc or mtype not in MEMORY_TYPES:
                    continue
                if self._store.get(path) is not None:
                    written.append(self._store.append(path, content).path)
                    continue
                near = self._store.similar(name, desc)
                if near is not None and near[1] >= SIMILAR_THRESHOLD:
                    written.append(self._store.append(near[0].slug, content).path)
                    continue
                written.append(self._store.write(path, name=name, description=desc, mtype=mtype, body=content,
                                                 topic=str(op.get("topic") or "")).path)
            except ValueError as exc:      # 文件名不合法等:跳过这一条
                _logger.info("记忆提取跳过一条:%s", exc)
        return written


def render_transcript(messages: tuple[MemoryMessage, ...]) -> str:
    """把一轮的消息压成纯文本:用户 / 助手原话,工具调用只留名字与参数摘要,结果截断;过长保留结尾。"""
    lines: list[str] = []
    for m in messages:
        if m.metadata.get("note_kind"):          # 系统注入的 note(日期、记忆索引等)不是对话内容
            continue
        content = str(m.content or "").strip()
        if m.role == "user" and content:
            lines.append(f"用户:{content}")
        elif m.role == "assistant":
            if content:
                lines.append(f"助手:{content}")
            for tc in m.metadata.get("tool_calls") or []:
                args = json.dumps(tc.get("input") or {}, ensure_ascii=False)[:200]
                lines.append(f"[调用 {tc.get('tool_name')} {args}]")
        elif m.role == "tool" and content:
            lines.append(f"[结果] {content[:TOOL_RESULT_CHARS]}")
    text = "\n".join(lines)
    return text[-MAX_TRANSCRIPT_CHARS:]


def parse_extraction(text: str) -> list[dict]:
    match = _JSON_RE.search(text)
    if match is None:
        raise ValueError(f"提取结果不是 JSON:{text[:200]!r}")
    items = json.loads(match.group(0)).get("memories") or []
    return [i for i in items if isinstance(i, dict)]
