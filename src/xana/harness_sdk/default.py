"""DefaultHarness — concrete turn orchestrator with a tool-use round-trip loop.

A single user turn now spans multiple `model.stream()` calls — the same shape
cc's query loop (claude-code-2.1.88-src/query.ts:1716) and yuxu_backup's
harness handler (services/llm_driver/handler.py:879-1359) use:

  1. assemble messages (system + history snapshot from ActiveContextStore)
  2. call `client.stream(model_request)` once, buffer tool_calls; do NOT
     dispatch mid-stream (would prevent the model from seeing tool results)
  3. on stream end:
       * append assistant MemoryMessage (text + tool_calls metadata)
       * if no tool_calls → break (final)
       * else dispatch each tool, append a MemoryMessage(role="tool",
         metadata={tool_use_id, tool_name, ok}) per result, loop back
  4. MAX_TURNS bounds the outer loop so a misbehaving model can't run forever

State authority is preserved: ActiveContextStore for context, HistoryMirror
for durable log, RunStore (via StateRepository) for run metadata.
"""
from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import itertools
import json
import logging
import time
from datetime import date
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator, AsyncIterator, Callable, Sequence

from xana.extension_api.assembler import PromptAssembler as ExtensionPromptAssembler
from xana.extension_api.contracts import (
    ApprovalReviewInput,
    ExtensionData,
    ThreadStartInput,
    ThreadStopInput,
    ToolFinishInput,
    ToolStartInput,
    TurnAbortInput,
    TurnInputContext,
    TurnStartInput,
    TurnStopInput,
)
from xana.extension_api.registry import ExtensionRegistry
from xana.harness_sdk.compact import (
    BLOCKING_HEADROOM, estimate_tokens,
    generate_summary, persist_compact_transcript, revealed_tools_from_messages,
)
from xana.harness_sdk.contracts import (
    HarnessContext,
    HarnessEvent,
    NOTIFY_USER_TOOL,
    HarnessRequest,
    ProgressEvent,
    PromptProfile,
    VerifyCheck,
)
from xana.harness_sdk.tool_batches import Progress, partition, run_batch
from xana.harness_sdk.goal import (
    GOAL_ACTIVATED,
    REPORT_GOAL_TOOL,
    evaluate_goal,
    goal_continue_text,
)
from xana.harness_sdk.interfaces import Harness
from xana.harness_sdk.context_notes import (
    PROJECT_INSTRUCTIONS_FILE,
    TODO_TOOL_NAME,
    TOOL_SEARCH_TOOL_NAME,
    CONTINUE_THINKING_ONLY,
    CONTINUE_TRUNCATED,
    NO_PROGRESS_REMINDER,
    changed_note,
    continuation_note,
    MAX_TURNS_SKIPPED,
    date_note,
    max_turns_text,
    project_instructions_note,
    deferred_tools_note,
    memory_index_note,
    memory_pinned_note,
    memory_recall_note,
    model_switch_text,
    stop_hook_text,
    todo_reminder_note,
)
from xana.harness_sdk.system_prompt import BASE_PROMPT_VAR, ENV_VAR, build_system_prompt, env_section, profile_sections
from xana.harness_sdk.ph_expand import expand_placeholders
from xana.harness_sdk.verify import failure_text, passed_text, run_checks
from xana.harness_sdk.usage import DefaultUsageAccumulator
from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.llm_driver.interfaces import ModelClient
from xana.memory.contracts import (ActiveContext, ExtractionRequest, HistoryRow, MemoryExtractor, MemoryMessage,
                                   Placeholder, ReplayResult)
from xana.memory.interfaces import ActiveContextStore, HistoryMirror
from xana.policy.contracts import PermissionContext, PermissionResult
from xana.state.contracts import RunRecord, UsageSnapshot
from xana.state.file_runs import save_prompt
from xana.tools.contracts import (
    ToolActivity, ToolContext, ToolEnvConfig, ToolInvocation, ToolResult, ToolSpec, Toolset,
)
from xana.harness_sdk.background import BackgroundTasks
from xana.tools.interfaces import Tool, ToolEnvironment
from xana.tools.toolsets import resolve_tools
from xana.config.paths import project_home


if TYPE_CHECKING:
    from xana.context.sdk import ContextSDK

_logger = logging.getLogger(__name__)
MEMORY_WRITE_TOOL = "memory_write"

_DEFAULT_PROVIDER = "fake"
_ANON_RUN_ID = "run-anon"
_ANON_CALL_ID = "call-anon"
# 续跑上限(照 Claude Code 2.1.281):只有思考推 1 次、截断续写 3 次、stop hook 连续拦截 8 次
_MAX_TRUNCATION_CONTINUES = 3
_MAX_STOP_HOOK_BLOCKS = 8
_SILENT_CALLS_BEFORE_REMINDER = 5
_MAX_SILENT_REMINDERS = 3


@dataclass
class _StopState:
    """一次 run 内的续跑计数。"""
    thinking_nudged: bool = False
    truncations: int = 0
    hook_blocks: int = 0
    stop_hooks_fired: bool = False
    # 目标模式:report_goal 声明的终态 / 最终判定(achieved / impossible / blocked / failed / unevaluated)
    goal_report: tuple[str, str] | None = None
    goal_status: str = ""
    verify_status: str = ""  # passed / failed(命令型验证)
    # 无进度提醒(照 CC 2.1.281 silent_turn_reminder):连续无可见输出的模型调用数 / 本静默期已提醒次数
    silent_calls: int = 0
    silent_reminders: int = 0
    background_nudges: int = 0  # 还有后台任务在跑却想结束时的续跑次数

# P5.2 — synthetic warning 中文文案,注入到 tool_result.content,告诉模型勿自动重试。
_SYNTHETIC_WARNING_TEXT_KILLED = (
    "[工具执行已被用户取消(超过 2 秒宽限期后强制终止)。该操作可能已部分完成 "
    "—— 切勿自动重试,半完成状态下重试可能让目标变得更糟。请等待用户的下一步指示。]"
)
_SYNTHETIC_WARNING_TEXT_READONLY = (
    "[只读工具被用户取消。该工具未产生副作用,可由用户决定是否在下一轮重试。]"
)
_TOOL_ABORT_GRACE_SECONDS = 2.0
_MAX_BACKGROUND_NUDGES = 20   # 无人接收结果的 run 等后台任务的续跑上限(每次一般是一次 task_wait)


def _available_tools(
    context: HarnessContext, specs: tuple[ToolSpec, ...], toolsets: tuple[Toolset, ...],
) -> frozenset[str] | None:
    """本 run 可用的工具名(agent 工具选择表达式解析结果,docs/design/agent-composition-design.md §3.3)。
    config_snapshot 没有 tools 键(直接使用 harness、不经 agent 层)→ None,不过滤;
    有 tool_ceiling(委派的子 run)时再与它取交集。"""
    ceiling = context.config_snapshot.get("tool_ceiling")
    if "tools" not in context.config_snapshot:
        return frozenset(ceiling) if ceiling is not None else None
    resolved = resolve_tools(context.config_snapshot["tools"] or (),
                             context.config_snapshot.get("disallowed_tools") or (), specs, toolsets)
    return resolved & frozenset(ceiling) if ceiling is not None else resolved  # 委派时不超出调用方给的上限


def _has_answer(produced: Sequence[MemoryMessage]) -> bool:
    """run 是否产出了模型的回答或工具结果(只有 note 不算)。"""
    return any(m.role in ("assistant", "tool") for m in produced)


def _has_env(context: HarnessContext) -> bool:
    """本 agent 的系统提示词是否含环境信息:默认提示词,或模板里放了 ${env} / ${base_prompt}。"""
    template = context.config_snapshot.get("agent_system_prompt")
    return not template or BASE_PROMPT_VAR in template or ENV_VAR in template


def _project_instructions(cwd: str | None) -> tuple[str, str] | None:
    """项目根目录 AGENTS.md 的 (路径, 内容);没有或读不了返回 None。"""
    if not cwd:
        return None
    path = Path(cwd) / PROJECT_INSTRUCTIONS_FILE
    try:
        return str(path), path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError) as exc:
        _logger.warning("读取项目说明 %s 失败:%s", path, exc)
        return None


def _visible_skills(context: HarnessContext) -> frozenset[str] | None:
    """agent 可见的技能名(config_snapshot["skills"]);None / 缺省 = 全部。"""
    skills = context.config_snapshot.get("skills")
    return frozenset(skills) if skills is not None else None


def _can_use(available: frozenset[str] | None, name: str) -> bool:
    return available is None or name in available


class DefaultHarness(Harness):
    def __init__(self, extension_registry: "ExtensionRegistry | None" = None,
                 today: Callable[[], str] | None = None,
                 memory_extractor: MemoryExtractor | None = None,
                 tool_environment: ToolEnvironment | None = None,
                 background: BackgroundTasks | None = None) -> None:
        """today:返回本地日期 YYYY-MM-DD(缺省系统时钟;测试可替换)。
        memory_extractor:每轮完成后在后台提取漏记的记忆(docs/design/memory-dream-design.md §3.1);None = 不提取。
        tool_environment:评测 run(request.metadata["tool_env"])的工具调用经它执行(回放 / 时点 / 拒绝);
        None = 只注入时点,工具原样执行。"""
        self._memory_extractor = memory_extractor
        self._tool_environment = tool_environment
        # 后台任务(超时转后台的 agent_run / eval_run):运行中把结束的任务作为注记插入;run 结束时登记,
        # 之后结束的任务改为通知会话
        self._background = background
        self._today = today or (lambda: date.today().isoformat())
        self.last_assembled_messages: tuple[ModelMessage, ...] | None = None
        self.usage_accumulator = DefaultUsageAccumulator()
        self._extensions = extension_registry
        self._thread_stores: dict[str, ExtensionData] = {}
        self._config_snapshots: dict[str, dict] = {}
        self._session_prompt_tokens: dict[tuple[str, str], tuple[int, int]] = {}  # key → (prompt_tokens, history_len_at_record)
        # Phase 3 Task 6: background async run tasks (kept for shutdown drain).
        self._background_tasks: "set[Any]" = set()
        # system 会话内固定(§5 turn-control-design):(session, agent) → (建立时的模型, 文本)。
        # 变化的上下文一律以 ContextNote 追加进历史,不再改 system。
        self._session_systems: dict[tuple[str, str], tuple[str, str]] = {}

    def forget_prompt_tokens(self, session_id: str, agent_name: str) -> None:
        """/clear 后丢弃该会话记下的上一轮 prompt 占用,避免按旧值误判压缩。"""
        self._session_prompt_tokens.pop((session_id, agent_name), None)

    def _session_system(
        self,
        context: HarnessContext,
        session_id: str,
        agent_name: str,
        extension_system_text: str,
        history: tuple[MemoryMessage, ...],
        *,
        memory: bool = False,
        memory_saving: bool = False,
    ) -> tuple[str, list[MemoryMessage]]:
        """本会话固定的 system;会话内换模型时不改 system,改为追加 model_switch note。"""
        cached = self._session_systems.get((session_id, agent_name))
        if cached is None:
            text = build_system_prompt(
                template=context.config_snapshot.get("agent_system_prompt") or None,
                env=env_section(cwd=context.cwd or None, model=context.model),
                extension_sections=extension_system_text,
                profile_tail=profile_sections(_profile(context)),
                memory=memory,
                memory_saving=memory_saving,
            )
            self._session_systems[(session_id, agent_name)] = (context.model, text)
            return text, []
        model, text = cached
        notes: list[MemoryMessage] = []
        switched_before = any(m.metadata.get("note_kind") == "model_switch" for m in history)
        if context.model != model or switched_before:
            switch = changed_note("model_switch", model_switch_text(context.model), history)
            if switch is not None:
                notes.append(switch)
        return text, notes

    async def _append_note(
        self,
        context: HarnessContext,
        session_id: str,
        agent_name: str,
        run_id: str,
        note: MemoryMessage,
        seq: str,
    ) -> None:
        assert context.active_context_store is not None and context.history_mirror is not None
        await context.active_context_store.append_message(session_id, agent_name, note)
        await context.history_mirror.append(HistoryRow(
            row_id=f"{run_id}:note:{seq}", session_id=session_id, agent_name=agent_name,
            kind="user", payload=note, ts=time.time(),
        ))

    async def close_thread(self, session_id: str) -> None:
        """Fire on_thread_stop and drop the thread-scoped extension store.

        Phase 1 note: no production caller exists yet; runtime/adapters are
        expected to call this on session teardown (Phase 2).
        """
        store = self._thread_stores.pop(session_id, None)
        self._config_snapshots.pop(session_id, None)
        if self._extensions is None or store is None:
            return
        for c in self._extensions.thread_lifecycle:
            await self._safe_hook(
                c.on_thread_stop(ThreadStopInput(session_id=session_id, thread_store=store)),
                "on_thread_stop",
            )

    async def _safe_hook(self, coro, hook_name: str) -> None:
        # Policy: lifecycle *observer* hooks (on_thread_start/stop,
        # on_turn_start/stop/abort, on_config_changed) are guarded — an
        # exception is logged and skipped so a misbehaving extension cannot
        # kill the turn. Data-producing contributors (contribute_prompt,
        # contribute_turn_input, tools, review) stay fail-fast because their
        # output shapes the run.
        try:
            await coro
        except Exception:
            _logger.exception("extension hook %s failed", hook_name)

    async def _stop_decision(
        self,
        ext: "ExtensionRegistry | None",
        st: _StopState,
        *,
        session_id: str,
        agent_name: str,
        run_id: str,
        thread_store: ExtensionData | None,
        turn_store: ExtensionData | None,
        turn_text: str,
        had_thinking: bool,
        stop_reason: str,
        final_text: str,
        goal_condition: str = "",
        goal_client: ModelClient | None = None,
        provider: str = "",
        context: HarnessContext | None = None,
    ) -> tuple[str, str] | None:
        """模型以纯文本结束本轮时的 Stop 决策:返回 (note kind, 续跑文本) 或 None=结束。

        顺序照 Claude Code 2.1.281:只有思考无可见输出 → 推 1 次;输出被截断 → 续写
        ≤3 次;Stop hook(``on_turn_stop`` 返回理由)→ 连续拦截 ≤8 次。"""
        if not turn_text.strip() and had_thinking and not st.thinking_nudged:
            st.thinking_nudged = True
            return "continuation", CONTINUE_THINKING_ONLY
        if stop_reason == "length" and st.truncations < _MAX_TRUNCATION_CONTINUES:
            st.truncations += 1
            return "continuation", CONTINUE_TRUNCATED
        reason: str | None = None
        hooks = ext.turn_lifecycle if ext is not None else ()
        if hooks:
            assert thread_store is not None and turn_store is not None
        for c in hooks:
            try:
                got = await c.on_turn_stop(TurnStopInput(
                    session_id=session_id, agent_name=agent_name, run_id=run_id,
                    status="final", thread_store=thread_store, turn_store=turn_store,
                    final_text=final_text, continuation_active=st.hook_blocks > 0,
                ))
            except Exception:
                _logger.exception("extension hook on_turn_stop failed")
                continue
            if got and reason is None:
                reason = got
        if reason is not None and st.hook_blocks < _MAX_STOP_HOOK_BLOCKS:
            st.hook_blocks += 1
            return "stop_hook", stop_hook_text(reason)
        evidence: tuple[MemoryMessage, ...] = ()
        if reason is not None:
            _logger.warning("stop hook 连续 %d 次阻止本轮结束,已覆盖并结束本轮", st.hook_blocks)
        else:
            checks = _verify_checks(context) if context is not None else ()
            if checks:
                assert context is not None
                results = await _run_checks_abortable(checks, context)
                if results is None:  # 用户取消:命令已随进程组结束,本轮收尾
                    st.stop_hooks_fired = bool(hooks)
                    return None
                failed = next((r for r in results if not r.ok), None)
                if failed is not None:
                    st.verify_status = "failed"
                    if st.hook_blocks < _MAX_STOP_HOOK_BLOCKS:
                        st.hook_blocks += 1
                        return "verify", failure_text(failed)
                    _logger.warning("验证连续 %d 次未通过,已结束本轮等人查看", st.hook_blocks)
                else:
                    st.verify_status = "passed"
                    # 通过结果作为证据交给目标评估器(不写入历史)
                    evidence = (MemoryMessage(role="tool", content=passed_text(results)),)
            if goal_condition and st.verify_status != "failed":
                goal = await self._goal_decision(
                    st, goal_condition, goal_client, provider, context, evidence)
                if goal is not None:
                    return goal
        # 本轮就此结束,on_turn_stop 已以 final 调用过,收尾时不再重复触发
        st.stop_hooks_fired = bool(hooks)
        return None

    async def _goal_decision(
        self, st: _StopState, condition: str, client: ModelClient | None, provider: str,
        context: HarnessContext | None, evidence: tuple[MemoryMessage, ...] = (),
    ) -> tuple[str, str] | None:
        """目标模式(CC /goal):独立评估器判断条件;与 stop hook 共用连续拦截上限。"""
        if st.goal_report is not None and st.goal_report[0] in ("blocked", "failed"):
            st.goal_status = st.goal_report[0]
            return None
        if st.hook_blocks >= _MAX_STOP_HOOK_BLOCKS:
            st.goal_status = "unevaluated"
            _logger.warning("目标模式连续 %d 次未达成,已结束本轮等人查看", st.hook_blocks)
            return None
        assert client is not None and context is not None and context.active_context_store is not None
        snap = await context.active_context_store.get(context.session_id, context.agent_name)
        try:
            verdict = await evaluate_goal(
                client, provider=provider, model=context.model, condition=condition,
                history=(snap.messages if snap is not None else ()) + evidence)
        except Exception:
            _logger.exception("goal evaluator failed")
            st.goal_status = "unevaluated"
            return None
        if verdict.ok:
            st.goal_status = "achieved"
            return None
        if verdict.impossible:
            st.goal_status = "impossible"
            return None
        st.hook_blocks += 1
        return "goal", goal_continue_text(verdict.reason)

    async def _fire_turn_end(
        self,
        ext: "ExtensionRegistry | None",
        *,
        session_id: str,
        agent_name: str,
        run_id: str,
        thread_store: ExtensionData | None,
        turn_store: ExtensionData | None,
        status: str | None,
        abort_reason: str | None = None,
    ) -> None:
        if ext is None:
            return
        assert thread_store is not None and turn_store is not None
        if abort_reason is not None:
            for c in ext.turn_lifecycle:
                await self._safe_hook(
                    c.on_turn_abort(TurnAbortInput(
                        session_id=session_id, agent_name=agent_name, run_id=run_id,
                        reason=abort_reason, thread_store=thread_store, turn_store=turn_store)),
                    "on_turn_abort",
                )
            return
        for c in ext.turn_lifecycle:
            await self._safe_hook(
                c.on_turn_stop(TurnStopInput(
                    session_id=session_id, agent_name=agent_name, run_id=run_id,
                    status=status or "final", thread_store=thread_store, turn_store=turn_store)),
                "on_turn_stop",
            )

    async def _start_memory_extraction(self, context: HarnessContext, request: HarnessRequest,
                                       session_id: str, agent_name: str, run_id: str) -> None:
        """把本轮(用户消息起)交给后台记忆提取;本轮模型已经自己写过记忆就跳过(照 Claude Code)。
        agent_run 派出的子 agent 不提取(调用方已排除),定时任务等无人值守运行照常提取。"""
        if self._memory_extractor is None:
            return
        snap = await context.active_context_store.get(session_id, agent_name)
        msgs = snap.messages if snap is not None else ()
        start = next((i for i in range(len(msgs) - 1, -1, -1)
                      if msgs[i].role == "user" and not msgs[i].metadata.get("note_kind")
                      and msgs[i].content == request.user_input), None)
        turn = tuple(msgs[start:]) if start is not None else ()
        if not turn or any(tc.get("tool_name") == MEMORY_WRITE_TOOL
                           for m in turn for tc in (m.metadata.get("tool_calls") or [])):
            return
        job = ExtractionRequest(session_id=session_id, agent_name=agent_name, run_id=run_id, messages=turn,
                                interactive=_profile(context).mode == "interactive")

        async def _run() -> None:
            try:
                await self._memory_extractor(job)
            except Exception:
                _logger.exception("记忆提取失败(会话 %s)", session_id)

        task = asyncio.create_task(_run(), name=f"xana-memory-extract-{run_id}")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _report_compact(self, context: HarnessContext, session_id: str,
                              run_id: str, phase: str) -> None:
        if context.progress_sink is None:
            return
        await context.progress_sink.report(ProgressEvent(
            session_id=session_id, run_id=run_id, message="compact_progress",
            payload={"kind": "compact_progress", "session_id": session_id,
                     "run_id": run_id, "model": context.model, "phase": phase},
        ))

    async def _run_tool_with_abort(
        self,
        execute_coro_factory,
        *,
        abort,
        read_only: bool,
        tool: "Tool | None" = None,
        call_id: str = "",
    ) -> ToolResult:
        """abort 触发时:
           - 先调用 ``tool.cancel(call_id)`` 让工具做协作式清理(write 工具可在
             此设内部 flag 触发自身的 grace + 强杀循环;read_only 工具默认 no-op,
             由下方 task.cancel 兜底)。
           - read_only=True → 立即取消 task;返回 cancelled ToolResult
           - read_only=False → 等 2.0s 宽限;完成则正常写,超时强杀 + 注入 synthetic warning
        """
        task = asyncio.create_task(execute_coro_factory())
        poll = 0.02
        signaled = False
        while not task.done():
            if getattr(abort, "aborted", False):
                # 先发协作式取消信号(只发一次)。
                if not signaled and tool is not None:
                    signaled = True
                    with contextlib.suppress(Exception):
                        await tool.cancel(call_id)
                if read_only:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                    return ToolResult(
                        ok=False,
                        content=_SYNTHETIC_WARNING_TEXT_READONLY,
                        failure_kind="cancelled",
                        error="abort during read-only tool",
                    )
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(task), _TOOL_ABORT_GRACE_SECONDS
                    )
                    return result
                except asyncio.TimeoutError:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                    return ToolResult(
                        ok=False,
                        content=_SYNTHETIC_WARNING_TEXT_KILLED,
                        failure_kind="cancelled",
                        error="abort + 2s grace timeout",
                    )
            await asyncio.sleep(poll)
        return task.result()

    @staticmethod
    async def _resolve_tool(name: str, context: HarnessContext, ext_tools: dict[str, Tool]) -> Tool | None:
        tool = ext_tools.get(name)
        if tool is None and context.tool_registry is not None:
            tool = await context.tool_registry.get(name)
        return tool

    @classmethod
    async def _tool_parallel(cls, tc: dict[str, Any], context: HarnessContext,
                             ext_tools: dict[str, Tool]) -> bool:
        if tc.get("input_error"):
            return False
        tool = await cls._resolve_tool(tc["tool_name"], context, ext_tools)
        return tool is not None and tool.spec().concurrency == "parallel"

    @staticmethod
    async def _tool_display(tc: dict[str, Any], context: HarnessContext,
                            ext_tools: dict[str, Tool]) -> dict[str, Any] | None:
        tool = ext_tools.get(tc["tool_name"])
        if tool is None and context.tool_registry is not None:
            tool = await context.tool_registry.get(tc["tool_name"])
        display = tool.spec().display if tool is not None else None
        return display.to_payload(dict(tc["input"])) if display is not None else None

    async def _dispatch_tool(
        self,
        tc: dict[str, Any],
        context: HarnessContext,
        session_id: str,
        agent_name: str,
        run_id: str,
        *,
        ext_tools: dict[str, Tool],
        available: frozenset[str] | None,
        ext: "ExtensionRegistry | None",
        thread_store: ExtensionData | None,
        turn_store: ExtensionData | None,
        progress: Callable[[str | ToolActivity], None] | None = None,
        tool_env: ToolEnvConfig | None = None,
    ) -> tuple[ToolResult, str]:
        """Run one tool through the validate→permission→pre→execute→post
        pipeline, wrapped by extension tool-lifecycle hooks.

        Extension-contributed tools take precedence in lookup and dispatch
        even when the shared ToolRegistry is missing. Returns
        (result, rendered_text); any exception becomes a synthetic failure
        ToolResult so the loop keeps going and the model can react."""
        tool_name = tc["tool_name"]
        call_id = tc["call_id"]

        async def _fire_tool_start() -> None:
            if ext is None or not ext.tool_lifecycle:
                return
            assert thread_store is not None and turn_store is not None
            for c in ext.tool_lifecycle:
                await self._safe_hook(
                    c.on_tool_start(ToolStartInput(
                        session_id=session_id, run_id=run_id, tool_name=tool_name,
                        call_id=call_id, arguments=dict(tc["input"]),
                        thread_store=thread_store, turn_store=turn_store,
                    )),
                    "on_tool_start",
                )

        async def _fire_tool_finish(result: ToolResult, rendered: str) -> None:
            if ext is None or not ext.tool_lifecycle:
                return
            assert thread_store is not None and turn_store is not None
            for c in ext.tool_lifecycle:
                await self._safe_hook(
                    c.on_tool_finish(ToolFinishInput(
                        session_id=session_id, run_id=run_id, tool_name=tool_name,
                        call_id=call_id, ok=result.ok, error=result.error,
                        rendered=rendered,
                        thread_store=thread_store, turn_store=turn_store,
                    )),
                    "on_tool_finish",
                )

        await _fire_tool_start()

        if not _can_use(available, tool_name):
            result = ToolResult(ok=False, content=None, failure_kind="policy_forbidden",
                                error=f"工具 {tool_name} 不在本 agent 的可用范围内",
                                rendered=f"[工具 {tool_name} 不在本 agent 的可用范围内]")
            await _fire_tool_finish(result, result.rendered or "")
            return result, result.rendered or ""
        # provider 解析不出合法 arguments(如 kimi 的非 JSON 片段)→ 不执行,
        # 以失败结果回给模型让其重发,而不是静默用 {} 调用工具。
        if tc.get("input_error"):
            result = ToolResult(ok=False, content=None, error=tc["input_error"],
                                failure_kind="validation",
                                rendered=f"[invalid tool arguments] {tc['input_error']}")
            await _fire_tool_finish(result, result.rendered or "")
            return result, result.rendered or ""

        # Extension tools first; fall back to the shared registry. The
        # "registry missing / unknown tool" failures only apply when the
        # extension map also misses.
        tool: Tool | None = ext_tools.get(tool_name)
        if tool is None:
            if context.tool_registry is None:
                result = ToolResult(ok=False, content=None, error="tool_registry missing",
                                    failure_kind="unsupported", rendered="[tool_registry missing]")
                await _fire_tool_finish(result, result.rendered or "")
                return result, result.rendered or ""
            tool = await context.tool_registry.get(tool_name)
        if tool is None:
            result = ToolResult(ok=False, content=None, error=f"tool {tool_name!r} not registered",
                                failure_kind="not_found", rendered=f"[tool {tool_name!r} not found]")
            await _fire_tool_finish(result, result.rendered or "")
            return result, result.rendered or ""

        invocation = ToolInvocation(
            tool_name=tool_name,
            input=tc["input"],
            call_id=call_id,
            session_id=session_id,
            run_id=run_id,
        )
        tool_ctx = ToolContext(
            session_id=session_id,
            agent_name=agent_name,
            cwd=context.cwd,
            permission=context.permission or _default_permission(session_id),
            run_id=run_id,
            report_progress=progress,
            report_activity=progress,
            available_tools=available,
            visible_skills=_visible_skills(context),
            env=tool_env,
        )
        try:
            await tool.validate(invocation)
            # plan 模式(EnterPlanMode 持久化的 mode="plan" 经 ChatAgent 映射进
            # PermissionContext)在这里真正消费:非只读工具直接拒绝。
            if tool_ctx.permission.mode == "plan" and not getattr(tool.spec(), "read_only", False):
                perm = PermissionResult(
                    decision="deny",
                    reason=(
                        "plan 模式:只允许只读工具。完成设计、退出 plan 模式后再执行修改性操作。"
                    ),
                )
            else:
                perm = await tool.check_permission(invocation, tool_ctx)
            if perm.decision != "allow" and ext is not None and ext.approval_review:
                # reviewer sits inside the catch-all try, so exceptions become
                # a synthetic execution-failure ToolResult (first non-None override wins).
                assert thread_store is not None and turn_store is not None
                review_input = ApprovalReviewInput(
                    invocation=invocation, result=perm,
                    thread_store=thread_store, turn_store=turn_store,
                )
                for reviewer in ext.approval_review:
                    override = await reviewer.review(review_input)
                    if override is not None:
                        perm = override
                        break
            if perm.decision != "allow":
                result = _denied_result(perm)
            else:
                async def _real() -> ToolResult:
                    prepared = await tool.pre_tool_use(invocation, tool_ctx)
                    res = await self._run_tool_with_abort(
                        lambda: tool.execute(prepared, tool_ctx),
                        abort=context.abort,
                        read_only=bool(getattr(tool.spec(), "read_only", False)),
                        tool=tool,
                        call_id=call_id,
                    )
                    return await tool.post_tool_use(prepared, tool_ctx, res)
                # 评测 run:统一入口交给工具环境(回放 / 真实执行 / 拒绝,iteration-agent-design §6.3)
                if tool_env is not None and self._tool_environment is not None:
                    result = await self._tool_environment.execute(invocation, tool, tool_ctx, _real)
                else:
                    result = await _real()
            rendered = await tool.render_result(result)
        except Exception as exc:
            result = ToolResult(
                ok=False,
                content=None,
                error=str(exc),
                failure_kind="execution",
                rendered=f"[tool failed: {exc}]",
            )
            rendered = result.rendered or ""
        await _fire_tool_finish(result, rendered)
        return result, rendered

    def invoke(self, request: HarnessRequest, context: HarnessContext) -> AsyncIterator[HarnessEvent]:
        return self._invoke(request, context)

    async def _invoke(self, request: HarnessRequest, context: HarnessContext) -> AsyncGenerator[HarnessEvent, None]:
        session_id = request.session_id
        agent_name = request.agent_name

        # ------------------------------------------------------------------
        # Spec §1.5 — sync vs async唯一差异 = consumer。
        # PH ritual (snapshot → append user_msg → append PH) fires for BOTH
        # modes so /attach can see in-flight runs regardless of mode.
        # Only consumer differs: async fires-and-forgets bg task; sync awaits
        # the run inline. Both route writeback through write_queue (or fall
        # back to direct store call when wq is not wired in tests).
        # ------------------------------------------------------------------
        from xana.state.file_runs import new_run_id as _new_run_id
        import asyncio as _asyncio

        # Async mode requires writers wired via metadata; otherwise fall
        # back to a sync-mode-style PH ritual without per-run JSONL.
        async_writers_ok = False
        main_writer = context.metadata.get("main_writer")
        run_jsonl_writer = context.metadata.get("run_jsonl_writer")
        if context.async_mode:
            if main_writer is None or run_jsonl_writer is None:
                _logger.warning(
                    "async_mode set but main_writer/run_jsonl_writer missing in "
                    "ctx.metadata; falling back to sync PH ritual"
                )
            else:
                async_writers_ok = True

        # Without an ActiveContextStore there is no PH ritual to perform —
        # fall straight through to the legacy sync path of _run_query_loop.
        if context.active_context_store is None:
            async for ev in self._run_query_loop(request, context):
                yield ev
            return

        run_id = context.run_id or _new_run_id()

        # a) Snapshot REAL store BEFORE appending the user_msg. Keep
        # Placeholders (from OTHER concurrent runs) in the seed so the LLM
        # prompt can render them (ph_expand). This run's own PH is
        # appended AFTER snapshot, so it's never seen here.
        pre_snapshot = await context.active_context_store.snapshot(session_id, agent_name)
        pre_msgs = tuple(pre_snapshot)

        # b) Append user_msg + PH to the REAL store (UI sees both).
        user_msg = MemoryMessage(role="user", content=request.user_input)
        await context.active_context_store.append_message(session_id, agent_name, user_msg)
        if context.history_mirror is not None:
            await context.history_mirror.append(HistoryRow(
                row_id=f"{run_id}:user", session_id=session_id,
                agent_name=agent_name, kind="user", payload=user_msg, ts=time.time(),
            ))

        label = (request.user_input or "")[:12]
        ph = Placeholder(
            run_id=run_id, label=label, status="running",
            jsonl_path=Path(f"runs/{run_id}.jsonl"),
        )
        await context.active_context_store.append_placeholder(session_id, agent_name, ph)

        # c) Build forked store seeded with pre_snapshot + user_msg.
        fork_store = _ForkedActiveContextStore(
            session_id=session_id, agent_name=agent_name,
            seed_messages=pre_msgs + (user_msg,),
            sdk=context.active_context_store.sdk(),
        )

        # d) per-run JSONL mirror when wired (async writers ok), else keep
        #    the existing real mirror (sync mode without write_queue wiring).
        if async_writers_ok:
            jsonl_mirror: Any = _RunJsonlMirror(
                writer=run_jsonl_writer, run_id=run_id,
                session_id=session_id, agent_name=agent_name,
            )
        else:
            jsonl_mirror = context.history_mirror

        # e) forked context — async_mode=False so the recursive call drops
        #    into the sync path of _run_query_loop. _run_query_loop will NOT
        #    re-append user_msg (handled here in _invoke).
        forked_context = replace(
            context,
            active_context_store=fork_store,
            history_mirror=jsonl_mirror,
            run_id=run_id,
            async_mode=False,
        )

        async def _finalize_writeback(produced: tuple[MemoryMessage, ...], status: str) -> None:
            """Route PH replacement through write_queue (preferred) or fall
            back to direct store/main_writer write. Spec §1.6."""
            wq = context.metadata.get("write_queue")
            runs_dir = context.metadata.get("runs_dir")
            if wq is not None:
                try:
                    from xana.memory.runs_loader import (
                        runs_jsonl_to_memory_messages,
                    )
                    from xana.memory.write_queue import ReplaceItem
                    msgs: tuple[MemoryMessage, ...]
                    if runs_dir is not None:
                        msgs = runs_jsonl_to_memory_messages(
                            Path(runs_dir) / f"{run_id}.jsonl"
                        )
                    else:
                        msgs = ()
                    if not msgs:
                        msgs = tuple(produced)
                    await wq.put(ReplaceItem(
                        run_id=run_id,
                        session_id=session_id,
                        agent_name=agent_name,
                        messages=msgs,
                    ))
                except Exception:
                    _logger.exception("write_queue.put(ReplaceItem) failed")
                return
            if main_writer is not None:
                try:
                    await main_writer.enqueue_replace_placeholder(
                        session_id=session_id, agent_name=agent_name,
                        run_id=run_id, replace_with=tuple(produced),
                    )
                    return
                except Exception:
                    _logger.exception("main_writer.enqueue_replace_placeholder failed")
            # Last-resort: write directly to the real store so the PH does
            # not linger (covers sync-mode tests without any writer wiring).
            try:
                await context.active_context_store.replace_placeholder(
                    run_id, tuple(produced)
                )
            except Exception:
                _logger.exception("direct replace_placeholder fallback failed")

        if async_writers_ok:
            # ---- ASYNC: fork + background task + queued forwarding ----
            event_queue: _asyncio.Queue = _asyncio.Queue()
            _SENTINEL: Any = object()

            async def _bg_run() -> None:
                status: str = "completed"
                try:
                    async for ev in self._run_query_loop(request, forked_context):
                        await event_queue.put(ev)
                        if ev.kind == "error":
                            status = "failed"
                            # XANA_DEBUG: surface error payload immediately
                            # (default _logger handlers don't print to stderr)
                            import sys as _sys
                            print(
                                f"[HARNESS-ERROR] bg run_id={run_id} sid={session_id} "
                                f"payload={ev.payload!r}",
                                file=_sys.stderr, flush=True,
                            )
                        elif ev.kind == "cancelled":
                            status = "cancelled"
                            import sys as _sys
                            print(
                                f"[HARNESS-CANCELLED] bg run_id={run_id} sid={session_id} "
                                f"payload={ev.payload!r}",
                                file=_sys.stderr, flush=True,
                            )
                except Exception:
                    import sys as _sys
                    import traceback as _tb
                    print(
                        f"[HARNESS-CRASH] bg run_id={run_id} sid={session_id}:",
                        file=_sys.stderr, flush=True,
                    )
                    _tb.print_exc(file=_sys.stderr)
                    _logger.exception("async background _run_query_loop crashed")
                    status = "failed"
                finally:
                    await event_queue.put(_SENTINEL)
                    produced = fork_store.produced_messages()
                    if status not in ("completed", "cancelled") and not _has_answer(produced):
                        # 只产出了 note(日期、记忆召回等)也算没有回答:追加失败说明,别让失败悄悄消失
                        # (用户取消不算失败)。assistant 角色(同 CC 合成的 API 错误消息):system 角色进不了上下文缓冲。
                        failure = MemoryMessage(
                            role="assistant",
                            content=f"此回答未完成（status={status}）。",
                        )
                        produced = (*produced, failure)
                        try:
                            await jsonl_mirror.append(HistoryRow(
                                row_id=f"{run_id}:bg:fail",
                                session_id=session_id, agent_name=agent_name,
                                kind="assistant", payload=failure, ts=time.time(),
                            ))
                        except Exception:
                            _logger.exception("run_jsonl fail-row write failed")
                    try:
                        await run_jsonl_writer.finalize(run_id, status)
                    except Exception:
                        _logger.exception("run_jsonl_writer.finalize failed")
                    await _finalize_writeback(produced, status)
                    if self._background is not None:
                        await self._background.run_ended(run_id)

            task = _asyncio.create_task(_bg_run(), name=f"xana-async-run-{run_id}")
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

            yield HarnessEvent(
                kind="turn_started", session_id=session_id, run_id=run_id,
                payload={"async": True, "label": label},
            )
            while True:
                ev = await event_queue.get()
                if ev is _SENTINEL:
                    break
                yield ev
            return

        # ---- SYNC: drive _run_query_loop inline, then enqueue writeback ----
        status = "completed"
        try:
            async for ev in self._run_query_loop(request, forked_context):
                if ev.kind == "error":
                    status = "failed"
                    import sys as _sys
                    print(
                        f"[HARNESS-ERROR] sync run_id={run_id} sid={session_id} "
                        f"payload={ev.payload!r}",
                        file=_sys.stderr, flush=True,
                    )
                elif ev.kind == "cancelled":
                    status = "cancelled"
                yield ev
        except Exception:
            import sys as _sys
            import traceback as _tb
            print(
                f"[HARNESS-CRASH] sync run_id={run_id} sid={session_id}:",
                file=_sys.stderr, flush=True,
            )
            _tb.print_exc(file=_sys.stderr)
            _logger.exception("sync _run_query_loop crashed")
            status = "failed"
            raise
        finally:
            produced = fork_store.produced_messages()
            if status not in ("completed", "cancelled") and not _has_answer(produced):
                produced = (*produced, MemoryMessage(
                    role="assistant",
                    content=f"此回答未完成（status={status}）。",
                ))
            await _finalize_writeback(produced, status)
            if self._background is not None:
                await self._background.run_ended(run_id)

    async def _run_query_loop(
        self, request: HarnessRequest, context: HarnessContext
    ) -> AsyncGenerator[HarnessEvent, None]:
        """Sync turn driver: the full single-turn query loop (system prompt
        assembly, model.stream(), tool round-trip, compaction, extension
        hooks). Both the sync path of _invoke and the async background task
        share this generator."""
        session_id = request.session_id
        agent_name = request.agent_name
        run_id = context.run_id or _ANON_RUN_ID

        # §4.3 预算护栏:budget 上限经 request.metadata 进入(同 parent_run_id
        # 的 lineage 通道);tracker 是 boot 组装的 concrete helper,经
        # ctx.metadata 注入(todo_store 同款松散注入)。
        _env = request.metadata.get("tool_env")
        tool_env: ToolEnvConfig | None = _env if isinstance(_env, ToolEnvConfig) else None
        run_budget = request.metadata.get("budget")
        if not isinstance(run_budget, dict):
            run_budget = {}
        budget_tracker = context.metadata.get("budget_tracker")
        budget_exceeded: str | None = None
        # §6.2 episode 状态机:软阈值 → landing(拒新 spawn/评估),
        # 硬阈值 → closing(当前 turn 收尾,同 budget_exceeded 语义优雅落地)。
        episode_runner = context.metadata.get("episode_runner")
        episode_exceeded: str | None = None

        if context.state_repository is not None:
            await context.state_repository.runs().create(
                RunRecord(
                    run_id=run_id, session_id=session_id, agent_name=agent_name,
                    status="running",
                    input_request={"text": request.user_input or ""},
                    label=(request.user_input or "")[:40],
                    parent_run_id=request.metadata.get("parent_run_id") or None,
                    budget=dict(run_budget),
                    metadata={"started_at": time.time()},
                )
            )

        ext = self._extensions
        thread_store: ExtensionData | None = None
        turn_store: ExtensionData | None = None
        if ext is not None:
            is_new_thread = session_id not in self._thread_stores
            thread_store = self._thread_stores.setdefault(session_id, ExtensionData(session_id))
            turn_store = ExtensionData(run_id)
            if is_new_thread:
                for c in ext.thread_lifecycle:
                    await self._safe_hook(
                        c.on_thread_start(ThreadStartInput(session_id=session_id, thread_store=thread_store)),
                        "on_thread_start",
                    )
            previous_cfg = self._config_snapshots.get(session_id)
            new_cfg = dict(context.config_snapshot)
            if previous_cfg is not None and previous_cfg != new_cfg:
                for c in ext.config:
                    await self._safe_hook(
                        c.on_config_changed(previous_cfg, new_cfg, thread_store),
                        "on_config_changed",
                    )
            self._config_snapshots[session_id] = new_cfg
            for c in ext.turn_lifecycle:
                await self._safe_hook(
                    c.on_turn_start(TurnStartInput(
                        session_id=session_id, agent_name=agent_name, run_id=run_id,
                        thread_store=thread_store, turn_store=turn_store,
                    )),
                    "on_turn_start",
                )

        assert context.active_context_store is not None, "ActiveContextStore required"
        assert context.history_mirror is not None, "HistoryMirror required"

        # NOTE: user_msg append + history mirror row are owned by _invoke
        # (PH ritual, spec §1.5). _run_query_loop runs against a forked
        # store that is already seeded with the user_msg.

        specs: tuple[ToolSpec, ...] = ()
        toolsets: tuple[Toolset, ...] = ()
        if context.tool_registry is not None:
            specs = tuple(await context.tool_registry.list_specs())
            toolsets = tuple(await context.tool_registry.list_toolsets())
        ext_tools: dict[str, Tool] = {}
        if ext is not None:
            for contributor in ext.tools:
                for tool in contributor.tools():
                    ext_tools[tool.spec().component.name] = tool
        # 本 agent 可用的工具;schema 过滤、执行前校验与下面各项注入都以它为准(§3.5)。
        available = _available_tools(
            context, specs + tuple(t.spec() for t in ext_tools.values()), toolsets)
        memory_tools = {sp.component.name for sp in specs if sp.toolset == "memory"}
        has_memory = context.memory_injector_registry is not None and (
            available is None or bool(available & memory_tools))
        visible_skills = _visible_skills(context)

        # §5 ContextNote:每条用户消息一次的上下文(记忆召回、技能/扩展片段)以 note
        # 追加在本条用户消息之后,只在有新内容时注入;system 不再承载它们。
        _snap = await context.active_context_store.get(session_id, agent_name)
        _hist: tuple[MemoryMessage, ...] = _snap.messages if _snap is not None else ()
        pending_notes: list[MemoryMessage] = []
        if _has_env(context):   # 日期随环境信息:完全替换提示词且不放 ${env} 的 agent 不注入
            # 评测 run 的"今天" = 研究时点(iteration-agent-design §6.3)
            today = date_note(tool_env.as_of if tool_env is not None and tool_env.as_of else self._today(), _hist)
            if today is not None:
                pending_notes.append(today)
            instructions = _project_instructions(context.cwd)
            if instructions is not None:
                note = project_instructions_note(*instructions, _hist)
                if note is not None:
                    pending_notes.append(note)
        if has_memory:   # 记忆随 memory 组:索引与置顶记忆强制注入(变化才注入),召回附在本条消息后
            blocks = await context.memory_injector_registry.collect(
                session_id, agent_name, request.user_input, context.cwd
            )
            by_place = {place: [b for b in blocks if b.placement == place] for place in ("index", "pinned", "recall")}
            if _has_env(context):
                for note in (memory_index_note("\n".join(b.content for b in by_place["index"]), _hist),
                             memory_pinned_note("\n".join(b.content for b in by_place["pinned"]), _hist)):
                    if note is not None:
                        pending_notes.append(note)
            recall = memory_recall_note(by_place["recall"], _hist)
            if recall is not None:
                pending_notes.append(recall)
        turn_input_parts: list[str] = []
        if ext is not None and ext.turn_input:
            tic = TurnInputContext(
                session_id=session_id, agent_name=agent_name, run_id=run_id,
                user_input=request.user_input, cwd=context.cwd or None, available_tools=available,
                visible_skills=visible_skills,
            )
            for c in ext.turn_input:
                for frag in await c.contribute_turn_input(tic, thread_store, turn_store):
                    turn_input_parts.append(frag.content)
        turn_input_note = changed_note(
            "turn_input", "\n\n".join(p for p in turn_input_parts if p), _hist)
        if turn_input_note is not None:
            pending_notes.append(turn_input_note)
        profile = _profile(context)
        goal_condition = ""
        if profile.goal_mode:
            goal_condition = str(request.metadata.get("goal") or request.user_input)
            goal_note = changed_note(
                "goal", GOAL_ACTIVATED.format(condition=goal_condition), _hist)
            if goal_note is not None:
                pending_notes.append(goal_note)
        run_started = time.monotonic()
        for n_idx, note in enumerate(pending_notes):
            await self._append_note(context, session_id, agent_name, run_id,
                                    note, f"pre:{n_idx}")
        # ContextContributor 片段进 system,契约要求会话内稳定;system 按会话缓存,
        # 首轮的输出即为本会话的版本。
        extension_system_text = ""
        if ext is not None and ext.context:
            extension_system_text = await ExtensionPromptAssembler(ext).assemble(
                thread_store=thread_store, turn_store=turn_store, base_parts=[],
            )
        todo_store = context.metadata.get("todo_store")
        # system prompt 与 tools_schema 在每轮 turn 循环内组装(deferred 揭示
        # 后下一轮即生效),这里只准备不变的输入。

        provider_opt = context.config_snapshot.get("provider")
        if provider_opt is None:
            # 缺 provider 多半是装配错误。fake 已注册说明调用方是有意的
            # (canonical fake pipeline / 测试),放行;否则报出真正的原因,
            # 而不是让人摸不着头脑的 "provider 'fake' not registered"。
            provider = _DEFAULT_PROVIDER
            _logger.warning(
                "config_snapshot 缺 provider — 回退到 %r;生产路径(ChatAgent)应始终填写",
                _DEFAULT_PROVIDER,
            )
        else:
            provider = str(provider_opt)
        if context.model_registry is None:
            await self._fire_turn_end(
                ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                thread_store=thread_store, turn_store=turn_store, status="error",
            )
            yield HarnessEvent(kind="error", session_id=session_id, run_id=run_id,
                               payload={"error": "model_registry missing"})
            return
        client = await context.model_registry.get(provider)
        if client is None:
            await self._fire_turn_end(
                ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                thread_store=thread_store, turn_store=turn_store, status="error",
            )
            if provider_opt is None:
                message = "config_snapshot.provider missing (no 'fake' fallback registered)"
            else:
                message = f"provider {provider!r} not registered"
            yield HarnessEvent(kind="error", session_id=session_id, run_id=run_id,
                               payload={"error": message})
            return

        usage = UsageSnapshot(model=context.model, provider=provider)
        # 回答文字 = 不带工具调用的那几轮(截断续写 / 续跑的多段回答仍拼接);
        # 与工具调用同轮的文字是进度说明,单独收集(CC:最终消息自足,中途文字是状态说明)
        answer_parts: list[str] = []
        progress_texts: list[str] = []
        turn_start = time.monotonic()
        compact_attempted = False

        # 早发一次 usage_progress:让 adapter footer 立刻拿到 model+ctx_max
        # (否则要等模型首个 usage 事件,footer 会有几秒空窗只有"🟡 回答用时 0s")。
        if context.progress_sink is not None:
            await context.progress_sink.report(ProgressEvent(
                session_id=session_id, run_id=run_id,
                message="usage_progress",
                payload={
                    "kind": "usage_progress",
                    "session_id": session_id,
                    "model": context.model,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "elapsed_s": 0,
                    "context_window": context.config_snapshot.get("context_window"),
                },
            ))

        # 工具轮上限:缺省不限(与 Claude Code / Kimi Code 一致),AgentSpec.max_turns 显式配置时生效;
        # 用完后多给一轮只写总结(wrap_up),仍调工具才报错。
        _limit = context.config_snapshot.get("max_turns")
        max_turns = int(_limit) if _limit else None
        stop_state = _StopState()
        for turn_index in (range(max_turns + 1) if max_turns is not None else itertools.count()):
            wrap_up = max_turns is not None and turn_index == max_turns
            if context.abort.aborted:
                await self._fire_turn_end(
                    ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                    thread_store=thread_store, turn_store=turn_store, status=None,
                    abort_reason=context.abort.reason or "aborted",
                )
                yield HarnessEvent(kind="cancelled", session_id=session_id, run_id=run_id,
                                   payload={"reason": context.abort.reason or "cancelled"})
                return
            snapshot = await context.active_context_store.get(session_id, agent_name)
            history_msgs = snapshot.messages if snapshot is not None else ()

            # ---- auto-compact: token 数 vs force_compact 线 ----
            ctx_win = context.config_snapshot.get("context_window")
            force_at = context.config_snapshot.get("force_compact")
            if ctx_win and force_at is not None and history_msgs:
                key = (session_id, agent_name)
                stored = self._session_prompt_tokens.get(key)
                if stored is not None and len(history_msgs) >= stored[1]:
                    tokens = stored[0]
                else:
                    # /clear 等导致历史变短 → 旧精确值失效,退回估算
                    tokens = estimate_tokens(history_msgs)
                if tokens >= int(force_at) and not compact_attempted:
                    compact_attempted = True
                    from xana.context._compact_debug import log_auto_compact
                    log_auto_compact(
                        session_id, run_id, tokens=tokens,
                        force_at=force_at, ctx_win=ctx_win,
                        history_len=len(history_msgs),
                    )
                    await self._report_compact(context, session_id, run_id, "start")
                    summary = await generate_summary(
                        client, provider=provider, model=context.model, messages=history_msgs)
                    if summary is None:
                        await self._report_compact(context, session_id, run_id, "failed")
                    else:
                        # 诚信修复:compact 前把将被压缩的消息区间落盘,摘要引用
                        # 真实可读路径;落盘失败退回旧式相对路径不阻断 compact。
                        transcript = await _persist_compact_transcript(
                            context, session_id, tuple(history_msgs))
                        if transcript is None:
                            transcript = f"runs/autocompact-{session_id}-{run_id}.jsonl"
                        # Compact-as-PH(§1.5 c3/c4):和手动 /compact 同一把 per-sid compact 锁;
                        # 替换成 CompactSummary 时 SDK 把它之前的历史折叠进摘要。
                        from xana.context._internal import _PlaceholderInternal
                        from xana.context.message import CompactSummary
                        sdk = context.active_context_store.sdk()
                        auto_run_id = f"autocompact-{session_id}-{run_id}"
                        try:
                            await sdk.append_compact_placeholder(
                                session_id, agent_name,
                                run_id=auto_run_id, ph_id=auto_run_id,
                                transcript_path=transcript,
                            )
                            await sdk.replace_placeholder(session_id, agent_name, auto_run_id, (CompactSummary(
                                summary=summary,
                                _internal=_PlaceholderInternal(
                                    run_id=auto_run_id, ph_id=auto_run_id, transcript_path=transcript),
                                revealed_tools=tuple(sorted(revealed_tools_from_messages(history_msgs))),
                            ),))
                        except Exception:
                            _logger.exception("auto-compact SDK writeback failed")
                            # 已插入的 compact 占位符要撤掉,否则 compact 锁不释放
                            await sdk.cancel_compact_placeholder(session_id, agent_name, auto_run_id)
                            await self._report_compact(context, session_id, run_id, "failed")
                        else:
                            self._session_prompt_tokens.pop(key, None)
                            await self._report_compact(context, session_id, run_id, "done")
                            snapshot = await context.active_context_store.get(session_id, agent_name)
                            history_msgs = snapshot.messages if snapshot is not None else ()
                elif tokens >= int(ctx_win) - BLOCKING_HEADROOM and compact_attempted:
                    await self._fire_turn_end(
                        ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                        thread_store=thread_store, turn_store=turn_store, status="error")
                    yield HarnessEvent(kind="error", session_id=session_id, run_id=run_id,
                                       payload={"error": "上下文已满且压缩未能降低占用,请 /clear 或更换模型"})
                    return

            # ---- per-turn tools_schema + system prompt(deferred 揭示后下轮生效)----
            revealed = revealed_tools_from_messages(history_msgs)  # 揭示状态存在历史里(CC 同款)
            if not _can_use(available, TOOL_SEARCH_TOOL_NAME):
                revealed = {sp.component.name for sp in specs}  # 没有 tool_search 就不隐藏任何工具
            schemas, deferred_names = _split_tool_schemas(specs, revealed)
            if not profile.goal_mode:  # report_goal 只在目标模式会话中出现
                schemas = [sc for sc in schemas if sc["name"] != REPORT_GOAL_TOOL]
                deferred_names = [d for d in deferred_names if d[0] != REPORT_GOAL_TOOL]
            if profile.mode != "unattended":  # notify_user 只在无人值守会话中出现
                schemas = [sc for sc in schemas if sc["name"] != NOTIFY_USER_TOOL]
                deferred_names = [d for d in deferred_names if d[0] != NOTIFY_USER_TOOL]
            if ext_tools:
                # extension 工具不参与 defer:永远全量 schema,且覆盖同名内置工具
                ext_tool_names = set(ext_tools)
                schemas = (
                    [s for s in schemas if s["name"] not in ext_tool_names]
                    + [_to_anthropic_tool_schema(t.spec()) for t in ext_tools.values()]
                )
                deferred_names = [d for d in deferred_names if d[0] not in ext_tool_names]
            # agent 的工具范围(AgentSpec.tools / disallowed_tools 解析结果)
            schemas = [sc for sc in schemas if _can_use(available, sc["name"])]
            deferred_names = [d for d in deferred_names if _can_use(available, d[0])]
            # 原生联网搜索互斥(同构 yuxu _maybe_inject_web_search):search_provider 为 native(或未设)且
            # provider 为 deepseek 系时,声明 yields_to_native 的搜索工具让位给模型端 server tool,
            # 从 schema 与 deferred 名单剔除,并经 metadata 通知 provider 注入。只在该工具仍在本 agent
            # 可用范围内时触发:白名单外的 agent 不会因此获得联网能力。
            request_metadata: dict[str, Any] = {}
            yielding = {sp.component.name for sp in specs if sp.yields_to_native}
            if (yielding and context.config_snapshot.get("search_provider") in (None, "", "native")
                    and provider.startswith("deepseek")
                    and (any(sc["name"] in yielding for sc in schemas)
                         or any(d[0] in yielding for d in deferred_names))):
                schemas = [sc for sc in schemas if sc["name"] not in yielding]
                deferred_names = [d for d in deferred_names if d[0] not in yielding]
                request_metadata["web_search_native"] = True
            # 前缀缓存路由键:同会话请求落到同一缓存(kimi prompt_cache_key)。
            request_metadata["prompt_cache_key"] = session_id
            # SessionConfig.thinking(bool)→ provider 方言开关;缺省不发,保持原行为
            if "thinking" in context.config_snapshot:
                request_metadata.setdefault(
                    "thinking",
                    "enabled" if context.config_snapshot.get("thinking") else "disabled")
            tools_schema: tuple[dict, ...] = tuple(schemas)
            system_content, loop_notes = self._session_system(
                context, session_id, agent_name, extension_system_text, history_msgs, memory=has_memory,
                memory_saving=has_memory and _profile(context).mode == "interactive")
            deferred_note = deferred_tools_note(tuple(deferred_names), history_msgs)
            if deferred_note is not None:
                loop_notes.append(deferred_note)
            if todo_store is not None and _can_use(available, TODO_TOOL_NAME):
                try:
                    todo_note = todo_reminder_note(history_msgs, todo_store.load(session_id))
                except Exception:
                    _logger.exception("todo_store.load failed; skipping todo reminder")
                    todo_note = None
                if todo_note is not None:
                    loop_notes.append(todo_note)
            if wrap_up:
                loop_notes.append(continuation_note("max_turns", max_turns_text(max_turns)))
            if self._background is not None:
                for rec in self._background.pop_finished_for_run(run_id):
                    loop_notes.append(continuation_note("background_done", rec.notification()))
            for n_idx, note in enumerate(loop_notes):
                await self._append_note(context, session_id, agent_name, run_id,
                                        note, f"{turn_index}:{n_idx}")

            # PH-first: re-read mixed snapshot (may contain Placeholders from
            # other concurrent runs) and render them per §1.3 before sending to
            # the LLM. The run never sees its own PH (fork snapshot was taken pre-append).
            _mixed_snap = await context.active_context_store.snapshot(session_id, agent_name)
            _prompt_msgs = expand_placeholders(_mixed_snap) if _mixed_snap else history_msgs
            assembled = _assemble_for_model(system_content, _prompt_msgs)
            self.last_assembled_messages = assembled

            model_request = ModelRequest(
                provider=provider,
                model=context.model,
                effort=context.effort,
                messages=assembled,
                tools_schema=tools_schema,
                metadata=request_metadata,
            )

            iter_text: list[str] = []
            iter_thinking: list[str] = []
            iter_tool_calls: list[dict[str, Any]] = []
            stream_error_yielded = False
            stop_reason = ""

            # trace(docs/design/iteration-agent-design.md §7):本次调用的耗时、首 token、用量、system 版本
            call_started = time.monotonic()
            first_token_ms: int | None = None
            call_usage: dict[str, Any] = {}
            # break 出循环时显式关闭 provider 流,避免由 GC 在别的上下文里 aclose。
            model_stream = client.stream(model_request)
            try:
                async for event in model_stream:
                    # 用户在 model streaming 中点取消 → 立刻退出生成循环,
                    # 否则要等模型自己 done 才被 outer loop 抓到,体感很慢。
                    if context.abort.aborted:
                        break
                    if first_token_ms is None and event.kind in ("text_delta", "tool_call"):
                        first_token_ms = int((time.monotonic() - call_started) * 1000)
                    if event.kind == "text_delta":
                        delta = event.payload.get("text", "")
                        # thinking-channel deltas stream out as model_delta
                        # events but never enter the final/persisted text.
                        if event.payload.get("channel") != "thinking":
                            iter_text.append(delta)
                        else:
                            # DS 工具轮次回放需要 reasoning_content —— 累积存
                            # metadata,但不进 final/persisted content。
                            iter_thinking.append(delta)
                        yield HarnessEvent(kind="model_delta", session_id=session_id,
                                           run_id=run_id, payload=dict(event.payload))
                    elif event.kind == "tool_call":
                        _tc: dict[str, Any] = {
                            "tool_name": event.payload.get("tool_name", ""),
                            "input": event.payload.get("input", {}) or {},
                            "call_id": event.payload.get("call_id", _ANON_CALL_ID),
                        }
                        if event.payload.get("input_error"):
                            _tc["input_error"] = event.payload["input_error"]
                        iter_tool_calls.append(_tc)
                    elif event.kind == "usage":
                        if event.usage is not None:
                            call_usage = {k: v for k, v in (
                                ("input", event.usage.input_tokens), ("output", event.usage.output_tokens),
                                ("cache_read", event.usage.cache_read_tokens),
                                ("cache_write", event.usage.cache_write_tokens)) if v is not None}
                        usage = await self.usage_accumulator.apply_model_event(usage, event)
                        # §4.3:RunRecord.budget 有上限的 run 每轮检查,超限置
                        # 收尾标记;循环在 assistant 落库后优雅退出(非裸死)。
                        if budget_tracker is not None and budget_exceeded is None and run_budget:
                            _reason = budget_tracker.check(
                                session_id=session_id, budget=run_budget, usage=usage,
                            )
                            if _reason is not None:
                                budget_exceeded = _reason
                                if context.progress_sink is not None:
                                    await context.progress_sink.report(ProgressEvent(
                                        session_id=session_id, run_id=run_id,
                                        message="budget_exceeded",
                                        payload={
                                            "kind": "budget_exceeded",
                                            "session_id": session_id,
                                            "run_id": run_id,
                                            "reason": _reason,
                                            "tokens_in": usage.exact_input_tokens or usage.estimated_input_tokens or 0,
                                            "tokens_out": usage.exact_output_tokens or usage.estimated_output_tokens or 0,
                                        },
                                    ))
                        if usage.exact_input_tokens:
                            self._session_prompt_tokens[(session_id, agent_name)] = (
                                usage.exact_input_tokens, len(history_msgs))
                        if episode_runner is not None and episode_exceeded is None:
                            _ep_tokens = (
                                (usage.exact_input_tokens or usage.estimated_input_tokens or 0)
                                + (usage.exact_output_tokens or usage.estimated_output_tokens or 0)
                            )
                            _ep_reason = await episode_runner.record_usage(_ep_tokens)
                            if _ep_reason is not None:
                                episode_exceeded = _ep_reason
                                if context.progress_sink is not None:
                                    await context.progress_sink.report(ProgressEvent(
                                        session_id=session_id, run_id=run_id,
                                        message="episode_threshold",
                                        payload={
                                            "kind": "episode_threshold",
                                            "session_id": session_id,
                                            "run_id": run_id,
                                            "reason": _ep_reason,
                                        },
                                    ))
                        if context.progress_sink is not None:
                            await context.progress_sink.report(ProgressEvent(
                                session_id=session_id, run_id=run_id,
                                message="usage_progress",
                                payload={
                                    "kind": "usage_progress",
                                    "session_id": session_id,
                                    "model": usage.model,
                                    "tokens_in": usage.exact_input_tokens or 0,
                                    "tokens_out": usage.exact_output_tokens or 0,
                                    "elapsed_s": time.monotonic() - turn_start,
                                    "context_window": context.config_snapshot.get("context_window"),
                                },
                            ))
                    elif event.kind == "done":
                        stop_reason = str(event.payload.get("stop_reason") or "")
                        break
                    elif event.kind == "rate_limited":
                        # provider 已放弃重试的限流:按错误收尾,避免静默空回复。
                        _rl = event.rate_limit
                        _rl_msg = "模型服务限流(HTTP 429)" + (
                            f": {_rl.reason}" if _rl is not None and _rl.reason else "")
                        await self._fire_turn_end(
                            ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                            thread_store=thread_store, turn_store=turn_store, status="error",
                        )
                        yield HarnessEvent(kind="error", session_id=session_id, run_id=run_id,
                                           payload={"error": _rl_msg})
                        stream_error_yielded = True
                        break
                    elif event.kind == "error" and event.error is not None:
                        # Fire turn-end BEFORE yielding so extensions see the
                        # error even if the consumer never resumes the generator.
                        await self._fire_turn_end(
                            ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                            thread_store=thread_store, turn_store=turn_store, status="error",
                        )
                        yield HarnessEvent(kind="error", session_id=session_id, run_id=run_id,
                                           payload={"error": event.error.message})
                        stream_error_yielded = True
                        break
            except Exception as exc:
                await self._fire_turn_end(
                    ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                    thread_store=thread_store, turn_store=turn_store, status="error",
                )
                yield HarnessEvent(kind="error", session_id=session_id, run_id=run_id,
                                   payload={"error": str(exc)})
                return
            finally:
                if isinstance(model_stream, collections.abc.AsyncGenerator):
                    await model_stream.aclose()

            if stream_error_yielded:
                return  # turn-end already fired before the error yield

            turn_text = "".join(iter_text)
            if turn_text:
                (progress_texts if iter_tool_calls else answer_parts).append(turn_text)
            asst_metadata: dict[str, Any] = {"tool_calls": list(iter_tool_calls)} if iter_tool_calls else {}
            if iter_thinking:
                asst_metadata["reasoning_content"] = "".join(iter_thinking)
            if turn_text or iter_tool_calls or iter_thinking:
                asst_msg = MemoryMessage(role="assistant", content=turn_text, metadata=asst_metadata)
                await context.active_context_store.append_message(session_id, agent_name, asst_msg)
                await context.history_mirror.append(
                    HistoryRow(
                        row_id=f"{run_id}:assistant:{turn_index}",
                        session_id=session_id,
                        agent_name=agent_name,
                        kind="assistant",
                        payload=asst_msg,
                        ts=time.time(),
                        metadata={"trace": {
                            "model": context.model, "provider": provider,
                            "latency_ms": int((time.monotonic() - call_started) * 1000),
                            "first_token_ms": first_token_ms,
                            "usage": call_usage, "stop_reason": stop_reason,
                            "system_sha": _save_system_prompt(context, system_content),
                        }},
                    )
                )

            # 无工具需配对时直接取消收尾；收尾轮取消也不能落入 max_turns 错误。
            if context.abort.aborted and not iter_tool_calls:
                await self._fire_turn_end(
                    ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                    thread_store=thread_store, turn_store=turn_store, status=None,
                    abort_reason=context.abort.reason or "aborted",
                )
                yield HarnessEvent(kind="cancelled", session_id=session_id, run_id=run_id,
                                   payload={"reason": context.abort.reason or "cancelled"})
                return

            # §4.3/§6.2 收尾:预算/episode 硬阈值超限 → 不再开新轮/派发工具,
            # 带已产出文本落地。
            if budget_exceeded is not None or episode_exceeded is not None:
                break
            if wrap_up:
                if not iter_tool_calls:
                    break                     # 收尾总结即最终回答
                for call_idx, tc in enumerate(iter_tool_calls):   # 不再执行;补上结果保持历史成对
                    skipped = MemoryMessage(role="tool", content=MAX_TURNS_SKIPPED, metadata={
                        "tool_use_id": tc["call_id"], "tool_name": tc["tool_name"], "ok": False})
                    await context.active_context_store.append_message(session_id, agent_name, skipped)
                    await context.history_mirror.append(HistoryRow(
                        row_id=f"{run_id}:tool:{turn_index}:{call_idx}", session_id=session_id,
                        agent_name=agent_name, kind="tool", payload=skipped, ts=time.time()))
                continue                      # 循环结束 → 报 max_turns 错误

            if not iter_tool_calls and self._background is not None and (
                    request.metadata.get("parent_run_id") or profile.mode == "unattended"):
                pending = self._background.running_for_run(run_id)
                if pending and stop_state.background_nudges < _MAX_BACKGROUND_NUDGES:
                    stop_state.background_nudges += 1
                    ids = "、".join(r.id for r in pending)
                    await self._append_note(context, session_id, agent_name, run_id, continuation_note(
                        "background_pending",
                        f"你启动的后台任务 {ids} 还在运行,结束前要拿到它们的结果:用 task_wait 等待"
                        "(不需要就用 task_stop 停掉)。没人会在你结束后替你接收结果。"), f"{turn_index}:bg")
                    yield HarnessEvent(kind="continuation", session_id=session_id, run_id=run_id,
                                       payload={"reason": "background_pending", "text": ids})
                    continue
            if not iter_tool_calls:
                decision = await self._stop_decision(
                    ext, stop_state, session_id=session_id, agent_name=agent_name, run_id=run_id,
                    thread_store=thread_store, turn_store=turn_store, turn_text=turn_text,
                    had_thinking=bool(iter_thinking), stop_reason=stop_reason,
                    final_text="\n".join(answer_parts),
                    goal_condition=goal_condition, goal_client=client, provider=provider,
                    context=context,
                )
                if decision is None:
                    break
                note_kind, note_text = decision
                await self._append_note(context, session_id, agent_name, run_id,
                                        continuation_note(note_kind, note_text),
                                        f"{turn_index}:continue")
                yield HarnessEvent(kind="continuation", session_id=session_id, run_id=run_id,
                                   payload={"reason": note_kind, "text": note_text})
                continue

            # 进度说明:与 tool_calls 同一轮的文字(渠道可单独展示,不属于最终回答)
            if turn_text.strip():
                stop_state.silent_calls = 0
                stop_state.silent_reminders = 0
                yield HarnessEvent(kind="progress_update", session_id=session_id,
                                   run_id=run_id, payload={"text": turn_text})
            else:
                stop_state.silent_calls += 1
            # 同一轮的调用分批:连续的可并行调用(ToolSpec.concurrency == "parallel")并发执行,
            # 其余独占;完成事件按发出顺序交付,历史写回顺序与模型发出时一致(tool_batches.py)
            parallel_flags = [await self._tool_parallel(tc, context, ext_tools) for tc in iter_tool_calls]
            for batch in partition(parallel_flags):
                calls = [iter_tool_calls[i] for i in batch]
                for tc in calls:
                    yield HarnessEvent(
                        kind="tool_started",
                        session_id=session_id,
                        run_id=run_id,
                        payload={
                            "tool_name": tc["tool_name"],
                            "call_id": tc["call_id"],
                            "input": dict(tc["input"]),
                            # 渠道按显示模式分组计数用(docs/design/tool-display-modes.md)
                            "display": await self._tool_display(tc, context, ext_tools),
                            "view_mode": context.config_snapshot.get("view_mode"),
                        },
                    )

                def _dispatch(tc: dict[str, Any], progress: Callable[[str | ToolActivity], None]):
                    return self._dispatch_tool(
                        tc, context, session_id, agent_name, run_id,
                        ext_tools=ext_tools, available=available, ext=ext,
                        thread_store=thread_store, turn_store=turn_store,
                        progress=progress, tool_env=tool_env,
                    )

                async for item in run_batch(calls, _dispatch):
                    tc = calls[item.index]
                    if isinstance(item, Progress):
                        # 工具执行中的进度(report_progress)边执行边以 tool_progress 事件发出
                        update = item.update
                        progress_payload: dict[str, Any] = {"call_id": tc["call_id"], "tool_name": tc["tool_name"]}
                        if isinstance(update, ToolActivity):   # 结构化活动:渠道按分道累积成时间线
                            progress_payload.update(text=update.text, activity={
                                "lane": update.lane, "lane_title": update.lane_title,
                                "kind": update.kind, "text": update.text})
                        else:
                            progress_payload["text"] = update
                        yield HarnessEvent(kind="tool_progress", session_id=session_id, run_id=run_id,
                                           payload=progress_payload)
                        continue
                    call_idx = batch[item.index]
                    result, rendered, _tool_elapsed_ms = item.result, item.rendered, item.elapsed_ms
                    tool_meta: dict[str, Any] = {
                        "tool_use_id": tc["call_id"],
                        "tool_name": tc["tool_name"],
                        "ok": result.ok,
                    }
                    if result.metadata.get("revealed_tools"):
                        tool_meta["revealed_tools"] = list(result.metadata["revealed_tools"])
                    if result.metadata.get("goal_status"):
                        stop_state.goal_report = (str(result.metadata["goal_status"]),
                                                  str(result.metadata.get("goal_reason", "")))
                    model_content = rendered
                    if profile.time_signal != "off":
                        # 时间软信号(Anthropic 指南):写进工具结果、保留在历史中,不改 system
                        model_content += f"\n\n已用 {int(time.monotonic() - run_started)} 秒"
                    tool_msg = MemoryMessage(role="tool", content=model_content, metadata=tool_meta)
                    await context.active_context_store.append_message(session_id, agent_name, tool_msg)
                    await context.history_mirror.append(
                        HistoryRow(
                            row_id=f"{run_id}:tool:{turn_index}:{call_idx}",
                            session_id=session_id,
                            agent_name=agent_name,
                            kind="tool",
                            payload=tool_msg,
                            ts=time.time(),
                            metadata={"trace": {
                                "elapsed_ms": _tool_elapsed_ms,
                                **({"env": result.metadata["env"]} if result.metadata.get("env") else {}),
                                **({} if result.ok else {"error": result.error or "",
                                                         "failure_kind": result.failure_kind or ""}),
                            }},
                        )
                    )
                    # UI 展示可被 display_text 覆盖(如 tool_search 的友好文案);
                    # 模型上下文(MemoryMessage)仍存 rendered,完整 schema 不变。
                    display_text = result.metadata.get("display_text") or rendered
                    yield HarnessEvent(
                        kind="tool_finished",
                        session_id=session_id,
                        run_id=run_id,
                        payload={
                            "text": display_text,
                            "tool_name": tc["tool_name"],
                            "call_id": tc["call_id"],
                            "input": dict(tc["input"]),
                            "ok": result.ok,
                            "error": "" if result.ok else (result.error or ""),   # 卡片失败行的一句话摘要
                            "cancelled": result.failure_kind == "cancelled",
                            "elapsed_ms": _tool_elapsed_ms,
                        },
                    )
            # 长时间无进度:连续 5 次模型调用没有对用户输出 → 在工具结果后追加提醒,
            # 同一静默期最多 3 次;子 agent 不注入(照 CC)
            if (stop_state.silent_calls >= _SILENT_CALLS_BEFORE_REMINDER
                    and stop_state.silent_reminders < _MAX_SILENT_REMINDERS
                    and not request.metadata.get("parent_run_id")):
                stop_state.silent_calls = 0
                stop_state.silent_reminders += 1
                await self._append_note(context, session_id, agent_name, run_id,
                                        continuation_note("no_progress", NO_PROGRESS_REMINDER),
                                        f"{turn_index}:no_progress")
        else:
            await self._fire_turn_end(
                ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                thread_store=thread_store, turn_store=turn_store, status="error",
            )
            yield HarnessEvent(
                kind="error",
                session_id=session_id,
                run_id=run_id,
                payload={"error": f"max_turns ({max_turns}) exceeded — tool loop did not converge"},
            )
            return

        # 没有纯文字回答就结束(如预算在工具轮后耗尽)时,退回最后一条进度说明,避免空回复
        final_text = "\n".join(answer_parts) or "\n".join(progress_texts[-1:])
        if context.state_repository is not None:
            await context.state_repository.runs().update_usage(run_id, usage)
            # §4.1/§4.3 收尾写点:finished_at 支撑任务面板时长;budget_exceeded
            # 是预算护栏的收尾信号(阶段四 episode 状态机的消费方)。
            _fields: dict | None = None
            _rec = await context.state_repository.runs().get(run_id)
            if _rec is not None:
                _md = dict(_rec.metadata)
                _md["finished_at"] = time.time()
                if budget_exceeded is not None:
                    _md["budget_exceeded"] = budget_exceeded
                if episode_exceeded is not None:
                    _md["episode_exceeded"] = episode_exceeded
                _fields = {"metadata": _md}
            await context.state_repository.runs().update_status(
                run_id, "completed", fields=_fields
            )
        if budget_tracker is not None:
            # §4.3 跨 run 累计:per-session / per-day token 入账。
            budget_tracker.record(session_id, usage)
        if not stop_state.stop_hooks_fired:
            await self._fire_turn_end(
                ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                thread_store=thread_store, turn_store=turn_store, status="final",
            )
        if has_memory and not request.metadata.get("parent_run_id"):
            await self._start_memory_extraction(context, request, session_id, agent_name, run_id)
        yield HarnessEvent(
            kind="final",
            session_id=session_id,
            run_id=run_id,
            payload={"text": final_text, "progress": list(progress_texts),
                     **({"goal_status": stop_state.goal_status} if stop_state.goal_status else {}),
                     **({"verify_status": stop_state.verify_status} if stop_state.verify_status else {})},
            usage=usage,
        )


async def _run_checks_abortable(checks: tuple[VerifyCheck, ...], context: HarnessContext):
    """执行检查命令,同时轮询 AbortSignal;取消时结束命令并返回 None。"""
    task = asyncio.create_task(run_checks(checks, context.cwd))
    while not task.done():
        if context.abort.aborted:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return None
        await asyncio.wait({task}, timeout=0.2)
    return task.result()


def _verify_checks(context: HarnessContext) -> tuple[VerifyCheck, ...]:
    """agent 声明的回合结束检查命令(ChatAgent 从 AgentSpec.verify 写入 config_snapshot)。"""
    return tuple(VerifyCheck.from_raw(raw) for raw in context.config_snapshot.get("verify") or ())


def _profile(context: HarnessContext) -> PromptProfile:
    """会话的 PromptProfile(ChatAgent 在会话首次使用时固化进 config_snapshot)。"""
    raw = context.config_snapshot.get("prompt_profile")
    return PromptProfile.from_dict(raw) if raw else PromptProfile()


def _assemble_for_model(system_content: str, history: tuple[MemoryMessage, ...]) -> tuple[ModelMessage, ...]:
    """Turn the active-context history into ModelMessages, preserving the
    assistant/tool metadata that round-trip the tool_use cycle."""
    msgs: list[ModelMessage] = [ModelMessage(role="system", content=system_content)]
    for m in history:
        # compact 摘要按会话规格 §1.9 以 user 角色出现;其余未知角色归 system
        role = "user" if m.role == "compact" else m.role if m.role in ("user", "assistant", "system", "tool") else "system"
        msgs.append(ModelMessage(role=role, content=m.content, metadata=dict(m.metadata)))
    return tuple(msgs)


def _default_permission(session_id: str) -> PermissionContext:
    return PermissionContext(mode="default", session_id=session_id)


def _save_system_prompt(context: HarnessContext, text: str) -> str | None:
    """system prompt 存进 runs/prompts/(按哈希去重),trace 行记 sha;写失败不影响回答。"""
    try:
        return save_prompt(_sessions_root_from(context) / "runs", text)
    except OSError:
        _logger.warning("system prompt 存档失败", exc_info=True)
        return None


def _sessions_root_from(context: HarnessContext) -> Path:
    runs_dir = context.metadata.get("runs_dir")
    if runs_dir:
        return Path(runs_dir).parent
    return project_home() / "sessions"


async def _persist_compact_transcript(
    context: HarnessContext, session_id: str, messages: tuple
) -> str | None:
    return persist_compact_transcript(_sessions_root_from(context), session_id, messages)


def _to_anthropic_tool_schema(spec) -> dict:
    """Pack a ToolSpec into the {name, description, input_schema} shape the
    Anthropic /v1/messages `tools` array expects."""
    return {
        "name": spec.component.name,
        "description": spec.component.description or "",
        "input_schema": dict(spec.input_schema),
    }


def _split_tool_schemas(
    specs, revealed: set[str],
) -> tuple[list[dict], list[tuple[str, str]]]:
    """defer 且未揭示 → 只给 (名字, 描述);其余给完整 schema。"""
    schemas: list[dict] = []
    deferred: list[tuple[str, str]] = []
    for s in specs:
        if s.defer and s.component.name not in revealed:
            deferred.append((s.component.name, s.component.description or ""))
        else:
            schemas.append(_to_anthropic_tool_schema(s))
    return schemas, deferred




class _ForkedActiveContextStore(ActiveContextStore):
    """Private fork used by the async background task.

    Seeded with (pre_snapshot + user_msg). Appends after seeding that are
    role in {assistant, tool, system} are tracked as `produced` — these
    become the PH replacement payload posted back to the real store via
    MainWriter once the run completes.
    """

    def __init__(
        self,
        *,
        session_id: str,
        agent_name: str,
        seed_messages: tuple[MemoryMessage | Placeholder, ...],
        sdk: "ContextSDK",
    ) -> None:
        self._session_id = session_id
        self._agent_name = agent_name
        # Mixed list: other runs' Placeholders may be seeded from pre_snapshot
        # so the LLM prompt can show them (ph_expand). ActiveContext.messages
        # (returned by _ctx) stays pure MemoryMessage per the contract.
        self._messages: list[MemoryMessage | Placeholder] = list(seed_messages)
        self._produced: list[MemoryMessage] = []
        self._sdk = sdk

    def produced_messages(self) -> tuple[MemoryMessage, ...]:
        return tuple(self._produced)

    def sdk(self) -> "ContextSDK":
        return self._sdk

    def _ctx(self) -> ActiveContext:
        pure = tuple(m for m in self._messages if isinstance(m, MemoryMessage))
        return ActiveContext(
            session_id=self._session_id,
            agent_name=self._agent_name,
            messages=pure,
            token_estimate=sum(len(m.content) for m in pure),
        )

    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None:
        return self._ctx()

    async def set(self, context: ActiveContext) -> None:
        # 整体替换本 run 的视图;替换后的内容即本 run 的产出,随 PH 替换写回主历史。
        self._messages = list(context.messages)
        self._produced = list(context.messages)

    async def append_message(
        self, session_id: str, agent_name: str, message: MemoryMessage
    ) -> ActiveContext:
        self._messages.append(message)
        # 本 run 产出的消息随 PH 替换写回主历史;ContextNote 虽是 user 角色,
        # 也是本 run 追加的历史,必须写回(用户消息本身由 _invoke 直接写主历史)。
        if message.role in ("assistant", "tool", "system") or message.metadata.get("note_kind"):
            self._produced.append(message)
        return self._ctx()

    async def clear(self, session_id: str, agent_name: str) -> None:
        self._messages = []

    async def snapshot(
        self, session_id: str, agent_name: str
    ) -> tuple[MemoryMessage | Placeholder, ...]:
        return tuple(self._messages)

    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None:
        # Forked store should not see nested placeholders in normal flow.
        return None

    async def replace_placeholder(
        self, run_id: str, replace_with: tuple[MemoryMessage, ...]
    ) -> ActiveContext | None:
        return None

class _RunJsonlMirror(HistoryMirror):
    """HistoryMirror that writes each appended row into the per-run JSONL.

    snapshot/replay return empty — per-run JSONL is write-only from the
    harness perspective; the main HistoryMirror is the read authority.
    """

    def __init__(
        self,
        *,
        writer: Any,
        run_id: str,
        session_id: str,
        agent_name: str,
    ) -> None:
        self._writer = writer
        self._run_id = run_id
        self._session_id = session_id
        self._agent_name = agent_name

    async def append(self, row: HistoryRow) -> None:
        await self._writer.append(self._run_id, row)

    async def replay(self) -> ReplayResult:
        return ReplayResult(loaded_sessions=0, loaded_rows=0)

    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None:
        raise NotImplementedError("Group B/C")

    async def replace_placeholder(
        self, run_id: str, messages: tuple[MemoryMessage, ...]
    ) -> None:
        raise NotImplementedError("Group B/C")

def _denied_result(perm: PermissionResult) -> ToolResult:
    return ToolResult(
        ok=False,
        content=None,
        error=perm.reason or "permission denied",
        failure_kind="policy_forbidden",
        rendered=f"[denied: {perm.reason or 'no reason'}]",
    )
