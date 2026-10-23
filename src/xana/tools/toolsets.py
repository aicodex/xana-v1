"""工具组与 agent 工具选择表达式(docs/design/agent-composition-design.md §3.1–3.3)。

- ``BUILTIN_TOOLSETS``:内置工具组的权威表,由 boot() 登记进工具目录;附加项登记自己的组。
- ``resolve_tools``:把 agent 的工具选择表达式解析成具体工具名集合(纯函数)。

表达式元素:``default``(所有默认组)、组名、工具名、``*``(目录里全部工具);``()`` = 零工具。
结果再减去 disallowed(同样接受组名 / 工具名)。
"""
from __future__ import annotations

from collections.abc import Iterable

from xana.tools.contracts import ToolSpec, Toolset

DEFAULT = "default"
ALL = "*"

BUILTIN_TOOLSETS: tuple[Toolset, ...] = (
    Toolset("fs", "读写与搜索本地文件:Read / Write / Edit / Glob / Grep", default=True),
    Toolset("exec", "执行 shell 命令:Bash", default=True),
    Toolset("web", "联网搜索与读取网页", default=True),
    Toolset("memory", "检索与写入长期记忆", default=True),
    Toolset("session", "会话内工作流:todo、计划模式、worktree、目标汇报", default=True),
    Toolset("meta", "按需加载工具 schema 与技能说明:tool_search / skill_load", default=True),
    Toolset("research", "多步深度研究:deep_research / background_research", default=True),
    Toolset("schedule", "定时任务:cron_create / cron_list / cron_delete,无人值守时的 notify_user", default=True),
    Toolset("agents", "委派子 agent 与创建 agent:agent_run / agent_define", default=True),
    Toolset("projects", "管理 xana 项目(只在 default 项目):project_manage", default=True),
    Toolset("eval", "回看运行记录(trace_view),供迭代与排查;不在 default 内", default=False),
)


def resolve_tools(
    selection: Iterable[str],
    disallowed: Iterable[str],
    specs: Iterable[ToolSpec],
    toolsets: Iterable[Toolset],
) -> frozenset[str]:
    """工具选择表达式 → 工具名集合。只返回目录里存在的工具;不认识的名字忽略(boot 负责告警)。"""
    specs = tuple(specs)
    default_sets = {t.name for t in toolsets if t.default}
    by_set: dict[str, set[str]] = {}
    for sp in specs:
        if sp.toolset:
            by_set.setdefault(sp.toolset, set()).add(sp.component.name)
    names = {sp.component.name for sp in specs}

    def expand(items: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for item in items:
            if item == ALL:
                out |= names
            elif item == DEFAULT:
                for ts in default_sets:
                    out |= by_set.get(ts, set())
            elif item in by_set:
                out |= by_set[item]
            elif item in names:
                out.add(item)
        return out

    return frozenset(expand(selection) - expand(disallowed))


def unknown_names(
    items: Iterable[str], specs: Iterable[ToolSpec], toolsets: Iterable[Toolset],
) -> list[str]:
    """表达式里既不是关键字、组名,也不是已注册工具名的条目(按名精确匹配,常见为大小写笔误)。"""
    known = {DEFAULT, ALL} | {t.name for t in toolsets} | {sp.component.name for sp in specs}
    return sorted({i for i in items if i not in known})
