"""TapeToolEnvironment —— ToolEnvironment 首个实现:录制回放 / 真实执行(注入时点)/ 拒绝
(docs/design/iteration-agent-design.md §6.2)。

录制 = 以往 run 的 runs/<run_id>.jsonl(trace 本身就是磁带):按 (工具名, 规范化参数) 找相同调用,
回放当时给模型的工具结果。每次调用的策略按以下顺序判定:
  1. config.strategies 按工具名指定的策略;
  2. 录制里有相同调用 → replay;
  3. 有副作用(destructive)→ deny;
  4. 按本次调用的 temporal:static → live;as_of → live(时点经 ToolContext.env 注入);
     live / 未声明 → 有研究时点时 deny(只能取当前状态,会泄漏未来信息),否则 live;
  录制里找不到但指定了 replay → config.on_miss(live 计为回退)。
simulate 暂未实现(留接口),按 deny 处理。结果 metadata["env"] = {strategy, fallback}。
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from xana.state.file_runs import read_run_transcript
from xana.tools.contracts import EnvStrategy, ToolContext, ToolEnvConfig, ToolInvocation, ToolResult
from xana.tools.interfaces import Tool, ToolEnvironment

# harness 写进工具结果末尾的时间软信号,回放时去掉(新 run 会重新追加自己的)
_TIME_SIGNAL = re.compile(r"\n\n已用 \d+ 秒\Z")


def call_key(tool_name: str, tool_input: dict) -> str:
    return tool_name + "\x00" + json.dumps(tool_input, ensure_ascii=False, sort_keys=True)


def load_tape(runs_dir: Path, run_ids: tuple[str, ...]) -> dict[str, ToolResult]:
    """录制里每个 (工具名, 参数) 的首次结果。"""
    tape: dict[str, ToolResult] = {}
    for run_id in run_ids:
        loaded = read_run_transcript(runs_dir, run_id)
        if loaded is None:
            continue
        calls: dict[str, tuple[str, dict]] = {}
        for row in loaded[0]:
            p = row["payload"]
            meta = p.get("metadata") or {}
            for tc in meta.get("tool_calls") or ():
                calls[tc.get("call_id", "")] = (tc.get("tool_name", ""), tc.get("input") or {})
            if p.get("role") == "tool" and meta.get("tool_use_id") in calls:
                name, inp = calls[meta["tool_use_id"]]
                text = _TIME_SIGNAL.sub("", str(p.get("content") or ""))
                trace = (row.get("metadata") or {}).get("trace") or {}
                tape.setdefault(call_key(name, inp), ToolResult(
                    ok=bool(meta.get("ok", True)), content=text, rendered=text,
                    error=trace.get("error") or None, failure_kind=trace.get("failure_kind") or None))
    return tape


def _denied(tool_name: str, reason: str) -> ToolResult:
    return ToolResult(ok=False, content=None, error=reason, failure_kind="policy_forbidden",
                      rendered=f"[评测环境] 工具 {tool_name} 不可用:{reason}")


class TapeToolEnvironment(ToolEnvironment):
    def __init__(self, *, runs_dir: Path) -> None:
        self._runs_dir = Path(runs_dir)
        self._tapes: dict[tuple[str, ...], dict[str, ToolResult]] = {}

    def _tape(self, config: ToolEnvConfig) -> dict[str, ToolResult]:
        if config.tape not in self._tapes:
            self._tapes[config.tape] = load_tape(self._runs_dir, config.tape)
        return self._tapes[config.tape]

    def strategy(self, invocation: ToolInvocation, tool: Tool, config: ToolEnvConfig) -> tuple[EnvStrategy, str]:
        """(策略, 原因)。"""
        name = invocation.tool_name
        if name in config.strategies:
            return config.strategies[name], "按配置"
        if call_key(name, dict(invocation.input)) in self._tape(config):
            return "replay", "录制里有相同调用"
        if tool.spec().destructive:
            return "deny", "有副作用的工具在评测中不执行"
        temporal = tool.temporal_for(invocation)
        if temporal in ("static", "as_of"):
            return "live", f"temporal={temporal}"
        if config.as_of:
            return "deny", f"只能取当前状态(temporal={temporal or '未声明'}),在研究时点 {config.as_of} 下会泄漏未来信息"
        return "live", f"temporal={temporal or '未声明'},无研究时点"

    async def execute(self, invocation: ToolInvocation, tool: Tool, context: ToolContext,
                      run: Callable[[], Awaitable[ToolResult]]) -> ToolResult:
        config = context.env
        assert config is not None, "ToolEnvironment 只在评测 run 上调用"
        strategy, reason = self.strategy(invocation, tool, config)
        fallback = False
        if strategy == "replay":
            hit = self._tape(config).get(call_key(invocation.tool_name, dict(invocation.input)))
            if hit is not None:
                return _with_env(hit, "replay", False)
            if config.on_miss == "deny":
                return _with_env(_denied(invocation.tool_name, "录制里没有相同调用"), "deny", False)
            strategy, fallback = "live", True
        if strategy == "simulate":
            return _with_env(_denied(invocation.tool_name, "模拟策略尚未实现"), "deny", False)
        if strategy == "deny":
            return _with_env(_denied(invocation.tool_name, reason), "deny", False)
        return _with_env(await run(), "live", fallback)


def _with_env(result: ToolResult, strategy: str, fallback: bool) -> ToolResult:
    return replace(result, metadata={**result.metadata, "env": {"strategy": strategy, "fallback": fallback}})
