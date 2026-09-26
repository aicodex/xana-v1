"""工具过程的显示模式(docs/design/tool-display-modes.md)。

与渠道无关的纯函数:从回答区记录的过程步骤(进度说明 / 思考 / 工具调用)归并出过程组,
按显示模式的策略表生成"块"(标题 + 正文 + 是否折叠 + 是否失败),各渠道 renderer 只负责把块画出来。

- detailed:每次调用一个面板(现有的工具面板,renderer 走原路径)
- grouped: 每个过程组一个面板,标题(一行,截断)= 模型的进度说明或"搜索网页 3 次,打开 12 个网页";
  展开后先是完整说明和计数,再逐行列出调用;运行中的调用在面板下方单独一行显示
- summary: 运行中只有一行动态状态;完成后回答上方一行回顾,可展开看分组
失败的调用在 grouped / summary 下单独一行,不折叠。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from xana.agent_adapter.channels.unified_card import AnswerSection, ToolBlock

ViewMode = Literal["detailed", "grouped", "summary"]
VIEW_MODES: tuple[ViewMode, ...] = ("detailed", "grouped", "summary")
DEFAULT_IM_VIEW_MODE: ViewMode = "grouped"   # 飞书 / TG 默认;CLI 默认 detailed(UnifiedCardState 默认值)


@dataclass(frozen=True)
class ViewPolicy:
    unit: Literal["call", "group", "turn"]
    progress_line: bool      # 是否显示进度说明行(grouped 下进度说明已是组标题)
    live_detail: bool        # 运行中是否在标题/状态行附带当前调用
    max_groups: int = 0      # grouped:最多显示几组,更早的合并成一行;0 = 不限


POLICIES: dict[str, ViewPolicy] = {
    "detailed": ViewPolicy(unit="call", progress_line=True, live_detail=False),
    "grouped": ViewPolicy(unit="group", progress_line=False, live_detail=True, max_groups=6),
    "summary": ViewPolicy(unit="turn", progress_line=False, live_detail=True),
}

# 进行时文案按类别给(工具只声明完成态动词)
_RUNNING_VERB = {
    "web_search": "正在搜索网页", "web_open": "正在打开网页", "execute": "正在运行命令",
    "read": "正在读取文件", "edit": "正在修改文件", "search_code": "正在搜索代码",
    "memory": "正在查记忆", "research": "正在深度研究", "plan": "正在更新计划",
    "delegate": "正在委派任务", "ask": "正在等待回答",
}
MAX_TITLE_ITEMS = 4
LINE_MAX = 80
TITLE_MAX = 22   # 折叠面板标题保持一行(移动端飞书约 22 个汉字);完整说明与计数放进展开后的正文


@dataclass
class ProcessGroup:
    caption: str = ""
    members: list[ToolBlock] = field(default_factory=list)
    thinking: int = 0


@dataclass(frozen=True)
class Block:
    title: str
    body: str = ""
    folded: bool = True
    failed: bool = False


@dataclass(frozen=True)
class ProcessView:
    blocks: tuple[Block, ...] = ()
    line: str = ""           # summary:运行中的状态行 / 完成后的回顾行
    failed: tuple[str, ...] = ()


def policy_for(mode: str) -> ViewPolicy:
    return POLICIES.get(mode, POLICIES["detailed"])


def all_tools(sec: AnswerSection) -> list[ToolBlock]:
    return list(sec.tools) + list(sec._pending_tools_buf)


def build_groups(sec: AnswerSection) -> list[ProcessGroup]:
    """按记录的步骤归并:进度说明开启新组并作为组标题;思考并入当前组;工具调用进当前组。
    只有思考、没有工具调用的组不单独成组,思考并入下一组(照 DSH:思考跟随相邻的过程组)。"""
    by_id = {t.call_id: t for t in all_tools(sec) if t.call_id}
    groups: list[ProcessGroup] = []
    for kind, value in sec.steps:
        if kind == "caption":
            groups.append(ProcessGroup(caption=value))
            continue
        if not groups:
            groups.append(ProcessGroup())
        if kind == "think":
            groups[-1].thinking += 1
        elif kind == "tool" and value in by_id:
            groups[-1].members.append(by_id[value])
    merged: list[ProcessGroup] = []
    carry = 0
    for g in groups:
        if not g.members:
            carry += g.thinking
            continue
        g.thinking += carry
        carry = 0
        merged.append(g)
    if carry and merged:
        merged[-1].thinking += carry
    return merged


def _count_items(members: list[ToolBlock], thinking: int) -> list[str]:
    """按首次出现顺序累计:(动词, 单位) → 数量;思考排最后。"""
    order: list[tuple[str, str]] = []
    totals: dict[tuple[str, str], int] = {}
    for t in members:
        key = (t.verb or t.name, t.unit or "次")
        if key not in totals:
            order.append(key)
            totals[key] = 0
        totals[key] += max(1, t.count)
    items = [f"{verb} {totals[(verb, unit)]} {unit}" for verb, unit in order]
    if thinking:
        items.append(f"思考 {thinking} 次")
    return items


def counts_title(members: list[ToolBlock], thinking: int = 0) -> str:
    items = _count_items(members, thinking)
    if len(items) > MAX_TITLE_ITEMS:
        items = items[:MAX_TITLE_ITEMS] + [f"等 {len(items) - MAX_TITLE_ITEMS} 项"]
    return ",".join(items)


def _clip(text: str, n: int = LINE_MAX) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _fmt_s(secs: float | None) -> str:
    if secs is None:
        return ""
    if secs < 60:
        return f"{secs:.1f}s" if secs < 10 else f"{int(secs)}s"
    m, s = divmod(int(secs), 60)
    return f"{m}m{s:02d}s"


def running_text(t: ToolBlock) -> str:
    verb = _RUNNING_VERB.get(t.category, f"正在调用 {t.name}")
    return verb + (f" · {t.live}" if t.live else (f" · {t.summary}" if t.summary else ""))


def member_line(t: ToolBlock) -> str:
    icon = {"ok": "✓", "fail": "✗", "cancel": "⊘"}.get(t.status, "…")
    parts = [f"{icon} {t.verb or t.name}"]
    if t.summary:
        parts.append(t.summary)
    if t.status == "running" and t.live:
        parts.append(t.live)
    if t.elapsed_s is not None:
        parts.append(_fmt_s(t.elapsed_s))
    line = " · ".join(parts)
    if t.status == "fail" and t.body_md:
        line += f" — {_clip(t.body_md.strip().splitlines()[0], 60)}"
    return _clip(line, 120)


def _failed_lines(members: list[ToolBlock]) -> tuple[str, ...]:
    return tuple(member_line(t) for t in members if t.status == "fail")


def _running(members: list[ToolBlock]) -> ToolBlock | None:
    for t in reversed(members):
        if t.status == "running":
            return t
    return None


def build_view(sec: AnswerSection, mode: str, *, alive: bool) -> ProcessView:
    """alive = 回答仍在进行(streaming / detached)。detailed 返回空视图(renderer 走原工具面板)。"""
    policy = policy_for(mode)
    if policy.unit == "call":
        return ProcessView()
    groups = build_groups(sec)
    members = [t for g in groups for t in g.members]
    if not members and not any(g.thinking for g in groups):
        return ProcessView()
    failed = _failed_lines(members)
    if policy.unit == "group":
        blocks: list[Block] = []
        shown = groups
        if policy.max_groups and len(groups) > policy.max_groups:
            early = groups[: len(groups) - policy.max_groups]
            shown = groups[len(groups) - policy.max_groups:]
            early_members = [t for g in early for t in g.members]
            early_counts = counts_title(early_members, sum(g.thinking for g in early))
            blocks.append(Block(title=_clip(f"更早 {len(early)} 步:{early_counts}", TITLE_MAX),
                                body="\n".join([early_counts, ""] + [member_line(t) for t in early_members])))
        for g in shown:
            counts = counts_title(g.members, g.thinking)
            title = _clip(g.caption or counts, TITLE_MAX)
            head = ([g.caption] if g.caption else []) + [counts]
            body = "\n".join(head + [""] + [member_line(t) for t in g.members])
            blocks.append(Block(title=title, body=body, failed=any(t.status == "fail" for t in g.members)))
        run = _running(members) if (alive and policy.live_detail) else None
        line = _clip(running_text(run)) if run is not None else ""
        return ProcessView(blocks=tuple(blocks), line=line, failed=failed)
    # summary
    total = counts_title(members, sum(g.thinking for g in groups))
    if alive:
        run = _running(members)
        line = running_text(run) if run is not None else (total or "处理中")
        steps = len(members)
        return ProcessView(line=_clip(f"{line} · {steps} 步" if steps else line), failed=failed)
    body = "\n\n".join(
        (f"【{_clip(g.caption, 40)}】\n" if g.caption else "")
        + "\n".join(member_line(t) for t in g.members) for g in groups if g.members) or " "
    recap = total + (f"({len(failed)} 次失败)" if failed else "")
    return ProcessView(blocks=(Block(title=_clip(recap, TITLE_MAX), body=recap + "\n\n" + body),),
                       line=recap, failed=failed)
