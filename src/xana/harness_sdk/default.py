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

import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterator

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
    BLOCKING_HEADROOM, build_post_compact_context, estimate_tokens,
    generate_summary, messages_since_boundary, revealed_tools_from_messages,
)
from xana.harness_sdk.contracts import HarnessContext, HarnessEvent, HarnessRequest, ProgressEvent
from xana.harness_sdk.interfaces import Harness
from xana.harness_sdk.system_prompt import build_system_prompt, deferred_tools_section, env_section
from xana.harness_sdk.usage import DefaultUsageAccumulator
from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.memory.contracts import HistoryRow, MemoryMessage
from xana.policy.contracts import PermissionContext, PermissionResult
from xana.skills.contracts import SkillActivation
from xana.state.contracts import RunRecord, UsageSnapshot
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult
from xana.tools.interfaces import Tool


_logger = logging.getLogger(__name__)

_DEFAULT_PROVIDER = "fake"
_ANON_RUN_ID = "run-anon"
_ANON_CALL_ID = "call-anon"
_MAX_TURNS = 25  # cc's default; yuxu uses max_iterations=200 but caps differently per agent


class DefaultHarness(Harness):
    def __init__(self, extension_registry: "ExtensionRegistry | None" = None) -> None:
        self.last_assembled_messages: tuple[ModelMessage, ...] | None = None
        self.usage_accumulator = DefaultUsageAccumulator()
        self._extensions = extension_registry
        self._thread_stores: dict[str, ExtensionData] = {}
        self._config_snapshots: dict[str, dict] = {}
        self._session_prompt_tokens: dict[tuple[str, str], tuple[int, int]] = {}  # key → (prompt_tokens, history_len_at_record)

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

    async def _report_compact(self, context: HarnessContext, session_id: str,
                              run_id: str, phase: str) -> None:
        if context.progress_sink is None:
            return
        await context.progress_sink.report(ProgressEvent(
            session_id=session_id, run_id=run_id, message="compact_progress",
            payload={"kind": "compact_progress", "session_id": session_id,
                     "run_id": run_id, "model": context.model, "phase": phase},
        ))

    async def _dispatch_tool(
        self,
        tc: dict[str, Any],
        context: HarnessContext,
        session_id: str,
        agent_name: str,
        run_id: str,
        *,
        ext_tools: dict[str, Tool],
        ext: "ExtensionRegistry | None",
        thread_store: ExtensionData | None,
        turn_store: ExtensionData | None,
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
        )
        try:
            await tool.validate(invocation)
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
                prepared = await tool.pre_tool_use(invocation, tool_ctx)
                result = await tool.execute(prepared, tool_ctx)
                result = await tool.post_tool_use(prepared, tool_ctx, result)
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
        run_id = context.run_id or _ANON_RUN_ID
        session_id = request.session_id
        agent_name = request.agent_name

        if context.state_repository is not None:
            await context.state_repository.runs().create(
                RunRecord(run_id=run_id, session_id=session_id, agent_name=agent_name, status="running")
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

        user_msg = MemoryMessage(role="user", content=request.user_input)
        await context.active_context_store.append_message(session_id, agent_name, user_msg)
        await context.history_mirror.append(
            HistoryRow(
                row_id=f"{run_id}:user",
                session_id=session_id,
                agent_name=agent_name,
                kind="user",
                payload=user_msg,
                ts=0.0,
            )
        )

        injected_head: list[str] = []
        injected_tail: list[str] = []
        if context.memory_injector_registry is not None:
            blocks = await context.memory_injector_registry.collect(
                session_id, agent_name, request.user_input, context.cwd
            )
            for block in blocks:
                target = injected_tail if block.placement == "system_tail" else injected_head
                target.append(block.content)

        skill_blocks: list[str] = []
        if context.skill_registry is not None:
            activation = SkillActivation(
                kind="model",
                session_id=session_id,
                agent_name=agent_name,
                run_id=run_id,
                cwd=Path(context.cwd) if context.cwd else None,
            )
            skills = await context.skill_registry.discover(activation)
            for skill in skills:
                contribution = await skill.activate(activation)
                skill_blocks.extend(contribution.prompt_blocks)
        extension_tail: list[str] = []
        if ext is not None and ext.turn_input:
            tic = TurnInputContext(
                session_id=session_id, agent_name=agent_name, run_id=run_id,
                user_input=request.user_input, cwd=context.cwd or None,
            )
            for c in ext.turn_input:
                for frag in await c.contribute_turn_input(tic, thread_store, turn_store):
                    extension_tail.append(frag.content)
        base_parts = injected_head + skill_blocks + injected_tail + extension_tail
        if ext is not None and ext.context:
            dynamic_middle_text = await ExtensionPromptAssembler(ext).assemble(
                thread_store=thread_store, turn_store=turn_store, base_parts=base_parts,
            )
        else:
            dynamic_middle_text = "\n\n".join(p for p in base_parts if p)
        # TODO(claude_md): CC 把 CLAUDE.md 包 <system-reminder> 作为 messages[0]
        # prepend(PROMPT_ASSEMBLY_ORDER.md),不进 system prompt;xana 实现
        # 项目级规则文件加载后在这里 prepend 首条 user message。
        # system prompt 与 tools_schema 在每轮 turn 循环内组装(deferred 揭示
        # 后下一轮即生效),这里只准备不变的输入。

        provider = str(context.config_snapshot.get("provider", _DEFAULT_PROVIDER))
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
            yield HarnessEvent(kind="error", session_id=session_id, run_id=run_id,
                               payload={"error": f"provider {provider!r} not registered"})
            return

        specs: tuple = ()
        if context.tool_registry is not None:
            specs = tuple(await context.tool_registry.list_specs())
        ext_tools: dict[str, Tool] = {}
        if ext is not None:
            for contributor in ext.tools:
                for tool in contributor.tools():
                    ext_tools[tool.spec().component.name] = tool

        usage = UsageSnapshot(model=context.model, provider=provider)
        cumulative_text: list[str] = []
        turn_start = time.monotonic()
        compact_attempted = False

        for turn_index in range(_MAX_TURNS):
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
                live = messages_since_boundary(history_msgs)
                if tokens >= int(force_at) and live and not compact_attempted:
                    compact_attempted = True
                    await self._report_compact(context, session_id, run_id, "start")
                    summary = await generate_summary(
                        client, provider=provider, model=context.model, messages=live)
                    if summary is None:
                        await self._report_compact(context, session_id, run_id, "failed")
                    else:
                        await context.active_context_store.set(build_post_compact_context(
                            session_id=session_id, agent_name=agent_name, summary=summary,
                            revealed_tools=sorted(
                                revealed_tools_from_messages(history_msgs))))
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
            revealed = _revealed_tools(history_msgs)
            schemas, deferred_names = _split_tool_schemas(specs, revealed)
            if ext_tools:
                # extension 工具不参与 defer:永远全量 schema,且覆盖同名内置工具
                ext_tool_names = set(ext_tools)
                schemas = (
                    [s for s in schemas if s["name"] not in ext_tool_names]
                    + [_to_anthropic_tool_schema(t.spec()) for t in ext_tools.values()]
                )
                deferred_names = [d for d in deferred_names if d[0] not in ext_tool_names]
            # native web_search 互斥(同构 yuxu _maybe_inject_web_search):
            # search_provider 为 native(或未设)且 provider 为 deepseek 系 →
            # 函数版 web_search 整体让位给模型端 server tool,从 schema 与
            # deferred 名单一并剔除,并经 metadata 通知 provider 注入。
            request_metadata: dict[str, Any] = {}
            if (context.config_snapshot.get("search_provider") in (None, "", "native")
                    and provider.startswith("deepseek")):
                schemas = [s for s in schemas if s.get("name") != "web_search"]
                deferred_names = [d for d in deferred_names if d[0] != "web_search"]
                request_metadata["web_search_native"] = True
            # SessionConfig.thinking(bool)→ provider 方言开关;缺省不发,保持原行为
            if "thinking" in context.config_snapshot:
                request_metadata.setdefault(
                    "thinking",
                    "enabled" if context.config_snapshot.get("thinking") else "disabled")
            tools_schema: tuple[dict, ...] = tuple(schemas)
            system_content = build_system_prompt(
                identity=str(context.config_snapshot.get("agent_identity", "") or ""),
                override=context.config_snapshot.get("agent_system_prompt") or None,
                env=env_section(cwd=context.cwd or None, model=context.model),
                dynamic_middle=(
                    deferred_tools_section(tuple(deferred_names)),
                    dynamic_middle_text,
                ),
            )

            assembled = _assemble_for_model(system_content, history_msgs)
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

            try:
                async for event in client.stream(model_request):
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
                        iter_tool_calls.append({
                            "tool_name": event.payload.get("tool_name", ""),
                            "input": event.payload.get("input", {}) or {},
                            "call_id": event.payload.get("call_id", _ANON_CALL_ID),
                        })
                    elif event.kind == "usage":
                        usage = await self.usage_accumulator.apply_model_event(usage, event)
                        if usage.exact_input_tokens:
                            self._session_prompt_tokens[(session_id, agent_name)] = (
                                usage.exact_input_tokens, len(history_msgs))
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

            if stream_error_yielded:
                return  # turn-end already fired before the error yield

            turn_text = "".join(iter_text)
            if turn_text:
                cumulative_text.append(turn_text)
            asst_metadata: dict[str, Any] = {"tool_calls": list(iter_tool_calls)} if iter_tool_calls else {}
            if iter_thinking:
                asst_metadata["reasoning_content"] = "".join(iter_thinking)
            asst_msg = MemoryMessage(role="assistant", content=turn_text, metadata=asst_metadata)
            await context.active_context_store.append_message(session_id, agent_name, asst_msg)
            await context.history_mirror.append(
                HistoryRow(
                    row_id=f"{run_id}:assistant:{turn_index}",
                    session_id=session_id,
                    agent_name=agent_name,
                    kind="assistant",
                    payload=asst_msg,
                    ts=0.0,
                )
            )

            if not iter_tool_calls:
                break

            for call_idx, tc in enumerate(iter_tool_calls):
                yield HarnessEvent(
                    kind="tool_started",
                    session_id=session_id,
                    run_id=run_id,
                    payload={
                        "tool_name": tc["tool_name"],
                        "call_id": tc["call_id"],
                        "input": dict(tc["input"]),
                    },
                )
                result, rendered = await self._dispatch_tool(
                    tc, context, session_id, agent_name, run_id,
                    ext_tools=ext_tools, ext=ext,
                    thread_store=thread_store, turn_store=turn_store,
                )
                tool_meta: dict[str, Any] = {
                    "tool_use_id": tc["call_id"],
                    "tool_name": tc["tool_name"],
                    "ok": result.ok,
                }
                if result.metadata.get("revealed_tools"):
                    tool_meta["revealed_tools"] = list(result.metadata["revealed_tools"])
                tool_msg = MemoryMessage(role="tool", content=rendered, metadata=tool_meta)
                await context.active_context_store.append_message(session_id, agent_name, tool_msg)
                await context.history_mirror.append(
                    HistoryRow(
                        row_id=f"{run_id}:tool:{turn_index}:{call_idx}",
                        session_id=session_id,
                        agent_name=agent_name,
                        kind="tool",
                        payload=tool_msg,
                        ts=0.0,
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
                    },
                )
        else:
            await self._fire_turn_end(
                ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                thread_store=thread_store, turn_store=turn_store, status="error",
            )
            yield HarnessEvent(
                kind="error",
                session_id=session_id,
                run_id=run_id,
                payload={"error": f"max_turns ({_MAX_TURNS}) exceeded — tool loop did not converge"},
            )
            return

        final_text = "\n".join(cumulative_text)
        if context.state_repository is not None:
            await context.state_repository.runs().update_usage(run_id, usage)
            await context.state_repository.runs().update_status(run_id, "completed")
        await self._fire_turn_end(
            ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
            thread_store=thread_store, turn_store=turn_store, status="final",
        )
        yield HarnessEvent(
            kind="final",
            session_id=session_id,
            run_id=run_id,
            payload={"text": final_text},
            usage=usage,
        )


def _assemble_for_model(system_content: str, history: tuple[MemoryMessage, ...]) -> tuple[ModelMessage, ...]:
    """Turn the active-context history into ModelMessages, preserving the
    assistant/tool metadata that round-trip the tool_use cycle."""
    msgs: list[ModelMessage] = [ModelMessage(role="system", content=system_content)]
    for m in history:
        role = m.role if m.role in ("user", "assistant", "system", "tool") else "system"
        msgs.append(ModelMessage(role=role, content=m.content, metadata=dict(m.metadata)))
    return tuple(msgs)


def _default_permission(session_id: str) -> PermissionContext:
    return PermissionContext(mode="default", session_id=session_id)


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
        if getattr(s, "defer", False) and s.component.name not in revealed:
            deferred.append((s.component.name, s.component.description or ""))
        else:
            schemas.append(_to_anthropic_tool_schema(s))
    return schemas, deferred


# 揭示状态存在历史里(CC 同款思路);实现移到 compact.py 与 /compact 共用,留别名保持旧 import。
_revealed_tools = revealed_tools_from_messages


def _denied_result(perm: PermissionResult) -> ToolResult:
    return ToolResult(
        ok=False,
        content=None,
        error=perm.reason or "permission denied",
        failure_kind="policy_forbidden",
        rendered=f"[denied: {perm.reason or 'no reason'}]",
    )
