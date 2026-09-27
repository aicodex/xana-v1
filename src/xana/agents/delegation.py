"""委派规则(docs/design/agent-delegation-design.md §3.2–3.3):子 agent 的工具上限、有效工具、可委派列表。

agent_run 工具与可委派列表 note(AgentListingContributor)共用这里的规则,保证"列表上写的工具"与
"子 agent 实际拿到的工具"一致。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from xana.agents.contracts import AgentSpec
from xana.agents.interfaces import Agent
from xana.tools.contracts import ToolSpec, Toolset
from xana.tools.toolsets import resolve_tools

AGENT_RUN_TOOL = "agent_run"
AGENT_DEFINE_TOOL = "agent_define"

# 子 agent 永远拿不到的工具 / 组:委派与创建 agent(因此委派深度固定为 1),以及会话控制类工具。
SUBAGENT_EXCLUDED: tuple[str, ...] = (
    AGENT_RUN_TOOL, AGENT_DEFINE_TOOL, "EnterPlanMode", "EnterWorktree", "report_goal", "schedule",
)


def subagent_ceiling(
    caller_available: frozenset[str] | None, specs: Iterable[ToolSpec], toolsets: Iterable[Toolset],
) -> frozenset[str]:
    """子 agent 可用工具的上限 = 调用方本 run 可用的工具(None = 目录全部)− SUBAGENT_EXCLUDED。"""
    specs = tuple(specs)
    toolsets = tuple(toolsets)
    base = caller_available if caller_available is not None else frozenset(sp.component.name for sp in specs)
    return base - resolve_tools(SUBAGENT_EXCLUDED, (), specs, toolsets)


def effective_tools(
    spec: AgentSpec, ceiling: frozenset[str], specs: Iterable[ToolSpec], toolsets: Iterable[Toolset],
) -> frozenset[str]:
    """子 agent 实际拿到的工具 = 它自己的工具选择解析结果 ∩ 上限(harness 端同样取交集)。"""
    return resolve_tools(spec.tools, spec.disallowed_tools, specs, toolsets) & ceiling


def agent_listing(
    agents: Mapping[str, Agent], ceiling: frozenset[str],
    specs: Iterable[ToolSpec], toolsets: Iterable[Toolset],
) -> str:
    """可委派 agent 列表(按名字排序,内容只随 agent 定义与工具目录变化)。"""
    specs = tuple(specs)
    toolsets = tuple(toolsets)
    lines = []
    for name in sorted(agents):
        spec = agents[name].spec()
        tools = sorted(effective_tools(spec, ceiling, specs, toolsets))
        desc = spec.component.description
        if spec.when_to_use:
            desc = f"{desc}｜{spec.when_to_use}"
        lines.append(f"- {name}: {desc}(工具: {', '.join(tools) if tools else '无'})")
    return "\n".join(lines)
