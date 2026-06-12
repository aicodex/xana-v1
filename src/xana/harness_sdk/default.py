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

from xana.extension_api.assembler import PromptAssembler
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
from xana.harness_sdk.contracts import HarnessContext, HarnessEvent, HarnessRequest, ProgressEvent
from xana.harness_sdk.interfaces import Harness
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

    async def close_thread(self, session_id: str) -> None:
        """Fire on_thread_stop and drop the thread-scoped extension store."""
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
            system_content = await PromptAssembler(ext).assemble(
                thread_store=thread_store, turn_store=turn_store, base_parts=base_parts,
            )
        else:
            system_content = "\n\n".join(p for p in base_parts if p)

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

        tools_schema: tuple[dict, ...] = ()
        if context.tool_registry is not None:
            specs = await context.tool_registry.list_specs()
            tools_schema = tuple(_to_anthropic_tool_schema(s) for s in specs)
        ext_tools: dict[str, Tool] = {}
        if ext is not None:
            for contributor in ext.tools:
                for tool in contributor.tools():
                    ext_tools[tool.spec().component.name] = tool
            ext_tool_names = {name for name in ext_tools}
            tools_schema = tuple(
                [s for s in tools_schema if s["name"] not in ext_tool_names]
                + [_to_anthropic_tool_schema(t.spec()) for t in ext_tools.values()]
            )

        usage = UsageSnapshot(model=context.model, provider=provider)
        cumulative_text: list[str] = []
        turn_start = time.monotonic()

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
            assembled = _assemble_for_model(system_content, history_msgs)
            self.last_assembled_messages = assembled

            model_request = ModelRequest(
                provider=provider,
                model=context.model,
                messages=assembled,
                tools_schema=tools_schema,
            )

            iter_text: list[str] = []
            iter_tool_calls: list[dict[str, Any]] = []
            stream_error_yielded = False

            try:
                async for event in client.stream(model_request):
                    if event.kind == "text_delta":
                        delta = event.payload.get("text", "")
                        iter_text.append(delta)
                        yield HarnessEvent(kind="model_delta", session_id=session_id,
                                           run_id=run_id, payload={"text": delta})
                    elif event.kind == "tool_call":
                        iter_tool_calls.append({
                            "tool_name": event.payload.get("tool_name", ""),
                            "input": event.payload.get("input", {}) or {},
                            "call_id": event.payload.get("call_id", _ANON_CALL_ID),
                        })
                    elif event.kind == "usage":
                        usage = await self.usage_accumulator.apply_model_event(usage, event)
                        if context.progress_sink is not None:
                            await context.progress_sink.report(ProgressEvent(
                                session_id=session_id, run_id=run_id,
                                message="usage_progress",
                                payload={
                                    "kind": "usage_progress",
                                    "tokens_in": usage.exact_input_tokens or 0,
                                    "tokens_out": usage.exact_output_tokens or 0,
                                    "elapsed_s": time.monotonic() - turn_start,
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
                result, rendered = await self._dispatch_tool(
                    tc, context, session_id, agent_name, run_id,
                    ext_tools=ext_tools, ext=ext,
                    thread_store=thread_store, turn_store=turn_store,
                )
                tool_msg = MemoryMessage(
                    role="tool",
                    content=rendered,
                    metadata={
                        "tool_use_id": tc["call_id"],
                        "tool_name": tc["tool_name"],
                        "ok": result.ok,
                    },
                )
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
                yield HarnessEvent(
                    kind="tool_finished",
                    session_id=session_id,
                    run_id=run_id,
                    payload={
                        "text": rendered,
                        "tool_name": tc["tool_name"],
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


def _denied_result(perm: PermissionResult) -> ToolResult:
    return ToolResult(
        ok=False,
        content=None,
        error=perm.reason or "permission denied",
        failure_kind="policy_forbidden",
        rendered=f"[denied: {perm.reason or 'no reason'}]",
    )
