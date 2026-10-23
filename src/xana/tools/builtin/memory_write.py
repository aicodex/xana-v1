"""memory_write:项目记忆(memory/ 目录)的唯一写入口(docs/design/memory-recall-design.md §3.4)。

- create:name、description、type、content 必填;与已有记忆的名称 + 描述高度相似时拒绝,提示改用 update。
- update:改正文或 frontmatter(未给的字段保持原样)。
- delete:删除错误或过时的记忆。
memory/ 对 Write / Edit 受保护,本工具保证 frontmatter 完整;写入后增量更新记忆树索引(memory_search 可检索)。
"""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.memory.contracts import MEMORY_TYPES
from xana.memory.markdown import MarkdownMemoryTree
from xana.memory.store import ProjectMemoryStore
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

_ACTIONS = ("create", "update", "delete")
DUPLICATE_THRESHOLD = 0.6

_DESCRIPTION = (
    "保存、更新或删除本项目的记忆(memory/ 目录,跨会话保留;记忆索引每个会话自动注入,相关记忆按消息自动召回)。\n"
    "- create:新建一条。name(简短名称)、description(一句话说明,写清它在什么情况下有用,召回靠它)、"
    "type、content 必填;path 是文件名(小写字母 / 数字,可含 - _)。已有相近的记忆时会被拒绝,改用 update。\n"
    "- update:修改已有记忆;只传要改的字段。\n"
    "- delete:删除错误或过时的记忆。\n"
    "type:user(用户是谁、偏好)/ feedback(用户对做法的纠正或认可,写清原因与适用场景)/ "
    "project(进行中的工作、目标、约束,日期写绝对日期)/ reference(外部资源在哪)。"
    "topic 是可选主题(如 估值、产业链、选股),同类记忆用同一个主题,索引按它分组。"
    "pinned=true 表示每个会话都注入全文,只给短而关键、必须一直遵守的规则。"
)

_SPEC = ToolSpec(
    temporal="static",
    toolset="memory",
    component=ComponentSpec(
        kind="tool", name="memory_write", qualified_name="builtin.memory_write", description=_DESCRIPTION,
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_ACTIONS)},
            "path": {"type": "string", "description": "记忆文件名,如 user_preferences(可带 .md)"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "type": {"type": "string", "enum": list(MEMORY_TYPES)},
            "topic": {"type": "string", "description": "可选主题,如 估值 / 产业链 / 选股;同类记忆用同一个主题"},
            "content": {"type": "string", "description": "正文(Markdown)"},
            "pinned": {"type": "boolean"},
        },
        "required": ["action", "path"],
    },
    destructive=True,
    display=ToolDisplay(category="other", verb="写记忆", summary_args=("action", "path")),
)


class MemoryWriteTool(Tool):
    def __init__(self, *, store: ProjectMemoryStore, tree: MarkdownMemoryTree | None = None) -> None:
        """tree:写入后增量更新记忆树(memory_search 的索引);None = 不更新。"""
        self._store = store
        self._tree = tree

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        data = invocation.input
        if data.get("action") not in _ACTIONS:
            raise ValueError(f"action 只能是 {list(_ACTIONS)}")
        if data["action"] == "create":
            missing = [k for k in ("name", "description", "type", "content") if not str(data.get(k) or "").strip()]
            if missing:
                raise ValueError(f"create 需要 {', '.join(missing)}")
        if data.get("type") and data["type"] not in MEMORY_TYPES:
            raise ValueError(f"type 只能是 {list(MEMORY_TYPES)}")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="memory_write 是项目记忆的写入口")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        data = invocation.input
        try:
            if data["action"] == "create":
                return await self._create(data)
            if data["action"] == "update":
                return await self._update(data)
            return await self._delete(str(data["path"]))
        except ValueError as exc:
            return _fail(str(exc))

    async def _create(self, data: dict) -> ToolResult:
        slug = str(data["path"])
        if self._store.get(slug) is not None:
            return _fail(f"记忆 {slug} 已存在,改用 update")
        name, desc = str(data["name"]), str(data["description"])
        near = self._store.similar(name, desc)
        if near is not None and near[1] >= DUPLICATE_THRESHOLD:
            return _fail(f"已有相近的记忆 {near[0].path}(相似度 {near[1]:.2f}),改用 update 更新它")
        record = self._store.write(slug, name=name, description=desc, mtype=str(data["type"]),
                                   body=str(data["content"]), pinned=bool(data.get("pinned")),
                                   topic=str(data.get("topic") or ""))
        await self._reindex(record.path)
        return _ok(f"[memory_write] 已新建记忆 {record.path}({record.type}{',置顶' if record.pinned else ''})")

    async def _update(self, data: dict) -> ToolResult:
        old = self._store.get(str(data["path"]))
        if old is None:
            return _fail(f"记忆 {data['path']} 不存在,用 create 新建")
        record = self._store.write(
            old.slug, name=str(data.get("name") or old.name), description=str(data.get("description") or old.description),
            mtype=str(data.get("type") or old.type), body=str(data["content"]) if data.get("content") else old.body,
            pinned=bool(data["pinned"]) if "pinned" in data else old.pinned,
            topic=str(data["topic"]) if "topic" in data else old.topic)
        await self._reindex(record.path)
        return _ok(f"[memory_write] 已更新记忆 {record.path}")

    async def _delete(self, slug: str) -> ToolResult:
        record = self._store.get(slug)
        if record is None or not self._store.delete(slug):
            return _fail(f"记忆 {slug} 不存在")
        await self._reindex(record.path)
        return _ok(f"[memory_write] 已删除记忆 {record.path}")

    async def _reindex(self, rel: str) -> None:
        if self._tree is not None:
            await self._tree.reindex_paths((rel,))


def _ok(text: str) -> ToolResult:
    return ToolResult(ok=True, content=None, rendered=text)


def _fail(error: str) -> ToolResult:
    return ToolResult(ok=False, content=None, error=error, failure_kind="validation",
                      rendered=f"[memory_write 失败: {error}]")
