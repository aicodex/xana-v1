"""ContextNote 生成规则:会话内变化的上下文以 note 追加进历史,而不是改 system。

依据(见 docs/design/turn-control-design.md §5):Claude Code / Codex / Kimi Code 都把
每轮变化的上下文写入历史、只追加、只在变化时注入;system 会话内固定。这里的函数
都是纯函数:输入当前历史,返回要追加的 note(``MemoryMessage``)或 ``None``。
去重与计量全部通过扫描历史得出,compact 后旧 note 消失即自然重置。
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from xana.context.message import ContextNote
from xana.memory.contracts import InjectedBlock, MemoryMessage
from xana.memory.markdown import search_terms

if TYPE_CHECKING:
    from xana.tools.builtin._todo_store import TodoItem

# 记忆召回上限(照 Claude Code relevant_memories)
RECALL_MAX_FILES = 5
RECALL_MAX_SESSION_BYTES = 60 * 1024
RECALL_MIN_TERMS = 2  # CC 用"含空格"判断,中文无空格,改用检索词数

# todo 提醒节奏(照 Claude Code TODO_REMINDER_CONFIG)
TODO_TURNS_SINCE_WRITE = 10
TODO_TURNS_BETWEEN_REMINDERS = 10

TODO_TOOL_NAME = "TodoWrite"
READ_TOOL_NAME = "Read"
TOOL_SEARCH_TOOL_NAME = "tool_search"


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _note(kind: str, text: str, *, paths: tuple[str, ...] = (), size: int = 0) -> MemoryMessage:
    note = ContextNote(kind=kind, text=text, key=_key(text), paths=paths, size=size)
    return MemoryMessage(role="user", content=ContextNote.wrap(text),
                         metadata=note.legacy_metadata())


def _notes(history: Iterable[MemoryMessage], kind: str) -> list[dict[str, Any]]:
    return [m.metadata for m in history
            if m.role == "user" and m.metadata.get("note_kind") == kind]


def _last_key(history: Sequence[MemoryMessage], kind: str) -> str | None:
    found = _notes(history, kind)
    return str(found[-1].get("note_key", "")) if found else None


def _tool_calls(m: MemoryMessage) -> list[dict[str, Any]]:
    return list(m.metadata.get("tool_calls") or []) if m.role == "assistant" else []


# ---------------------------------------------------------------------------
# 记忆召回
# ---------------------------------------------------------------------------


def memory_recall_note(
    blocks: Sequence[InjectedBlock], history: Sequence[MemoryMessage], user_input: str,
    *, can_read: bool = True,
) -> MemoryMessage | None:
    """本条用户消息的记忆召回。同一记忆文件一个会话只注入一次;会话累计到上限即停。"""
    if len(set(search_terms(user_input))) < RECALL_MIN_TERMS:
        return None
    surfaced: set[str] = set()
    total = 0
    for md in _notes(history, "memory_recall"):
        surfaced.update(md.get("note_paths") or ())
        total += int(md.get("note_size", 0))
    if total >= RECALL_MAX_SESSION_BYTES:
        return None
    read_paths = [str((tc.get("input") or {}).get("file_path", ""))
                  for m in history for tc in _tool_calls(m)
                  if tc.get("tool_name") == READ_TOOL_NAME]
    entries: list[str] = []
    paths: list[str] = []
    for block in blocks:
        path = block.node_path
        if (not block.content.strip() or path in surfaced or path in paths
                or any(rp.endswith(path) for rp in read_paths if rp)):
            continue
        entries.append(f"- {path}:{' '.join(block.content.split())}")
        paths.append(path)
        if len(paths) >= RECALL_MAX_FILES:
            break
    if not entries:
        return None
    hint = "需要时用 Read 查看全文。" if can_read else ""
    text = f"以下记忆可能与当前请求相关(来自记忆树,只列标题与描述){hint}\n" + "\n".join(entries)
    return _note("memory_recall", text, paths=tuple(paths), size=len(text.encode("utf-8")))


# ---------------------------------------------------------------------------
# 技能 / 扩展片段、按需工具、模型切换:内容变化才追加
# ---------------------------------------------------------------------------


def changed_note(kind: str, text: str, history: Sequence[MemoryMessage]) -> MemoryMessage | None:
    """通用"变化才注入":与历史里同类最后一条 note 相同则跳过。空文本不注入。"""
    if not text.strip() or _last_key(history, kind) == _key(text):
        return None
    return _note(kind, text)


def deferred_tools_note(
    deferred: Sequence[tuple[str, str]], history: Sequence[MemoryMessage],
) -> MemoryMessage | None:
    """按需工具名单(名字 + 描述)。名单变化才追加;清空时若曾列出过,追加一句已全部加载。"""
    if deferred:
        lines = "\n".join(f"- {name}: {desc}" for name, desc in deferred)
        text = ("以下可用工具的 schema 尚未加载。在断定某项能力不存在或自行绕路之前,"
                "先用 tool_search 查看完整 schema 并按需加载:\n\n" + lines)
    elif _notes(history, "deferred_tools"):
        text = "此前列出的按需工具均已加载,当前没有待加载的工具。"
    else:
        return None
    return changed_note("deferred_tools", text, history)


def model_switch_text(model: str) -> str:
    return f"本会话此后由模型 {model} 继续(模型 ID 为 {model})。"


# ---------------------------------------------------------------------------
# 续跑提示(译自 Claude Code 2.1.281 主循环)
# ---------------------------------------------------------------------------

CONTINUE_THINKING_ONLY = "[你上一条回复没有可见输出。请继续,并给出用户可见的回复。]"
NO_PROGRESS_REMINDER = "用户已经有一段时间没收到你的消息了——用几句话说明你在做什么,然后继续。"
CONTINUE_TRUNCATED = ("达到输出 token 上限。直接续写,不道歉,不回顾你在做什么。"
                      "如果是在思路中间断开,就从中间接上。把剩余工作拆成更小的块。")


def stop_hook_text(reason: str) -> str:
    return f"Stop hook 反馈:{reason}"


def continuation_note(kind: str, text: str) -> MemoryMessage:
    """续跑指令同样以 note 追加(模型可见、非用户输入),不做"变化才注入"去重。"""
    return _note(kind, text)


# ---------------------------------------------------------------------------
# todo 提醒
# ---------------------------------------------------------------------------

_TODO_REMINDER = (
    "近期没有使用 TodoWrite 工具。如果你正在处理适合跟踪进度的任务,可以考虑用 TodoWrite "
    "记录进度;如果任务清单已经过时、与当前工作不符,也考虑清理它。只在与当前工作相关时使用。"
    "这只是一个温和的提醒——不适用就忽略。"
)


def todo_reminder_note(history: Sequence[MemoryMessage], todos: Sequence["TodoItem"]) -> MemoryMessage | None:
    """距上次 TodoWrite 与距上次提醒都满 10 个 assistant 轮次才提醒一次。"""
    since_write = since_reminder = 0
    write_seen = reminder_seen = False
    for m in reversed(history):
        if m.role == "user" and m.metadata.get("note_kind") == "todo_reminder":
            reminder_seen = True
        if m.role != "assistant":
            continue
        if any(tc.get("tool_name") == TODO_TOOL_NAME for tc in _tool_calls(m)):
            write_seen = True
        if not write_seen:
            since_write += 1
        if not reminder_seen:
            since_reminder += 1
    if since_write < TODO_TURNS_SINCE_WRITE or since_reminder < TODO_TURNS_BETWEEN_REMINDERS:
        return None
    text = _TODO_REMINDER
    if todos:
        items = "\n".join(f"{i}. [{t.status}] {t.content}" for i, t in enumerate(todos, start=1))
        text += "\n\n任务清单当前内容:\n" + items
    return _note("todo_reminder", text)
