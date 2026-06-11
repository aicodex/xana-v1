"""DefaultHarness — first concrete turn orchestrator.

Responsibilities (per design spec doc 2026-06-11):
  1. append user message to ActiveContextStore + HistoryMirror
  2. collect prompt blocks via MemoryInjectorRegistry
  3. discover + activate skills via SkillRegistry
  4. assemble ModelMessages (system header + history)
  5. stream the model (provider taken from context.config_snapshot["provider"])
  6. dispatch tool calls through the full Tool lifecycle
  7. accumulate usage
  8. emit ordered HarnessEvents: optional model_delta, tool_finished per call,
     final at the end. error on failure.
  9. append assistant message + mirror it

State authority is preserved: ActiveContextStore for context, HistoryMirror
for durable log, RunStore (via StateRepository) for run metadata.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import AsyncGenerator, AsyncIterator

from xana.harness_sdk.contracts import HarnessContext, HarnessEvent, HarnessRequest
from xana.harness_sdk.interfaces import Harness
from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.memory.contracts import HistoryRow, MemoryMessage
from xana.policy.contracts import PermissionContext, PermissionResult
from xana.skills.contracts import SkillActivation
from xana.state.contracts import RunRecord, UsageSnapshot
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult


_DEFAULT_PROVIDER = "fake"
_ANON_RUN_ID = "run-anon"
_ANON_CALL_ID = "call-anon"


class DefaultHarness(Harness):
    def __init__(self) -> None:
        self.last_assembled_messages: tuple[ModelMessage, ...] | None = None

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

        assert context.active_context_store is not None, "ActiveContextStore required"
        assert context.history_mirror is not None, "HistoryMirror required"

        user_msg = MemoryMessage(role="user", content=request.user_input)
        snapshot = await context.active_context_store.append_message(session_id, agent_name, user_msg)
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

        system_parts = injected_head + skill_blocks + injected_tail
        system_content = "\n\n".join(p for p in system_parts if p)

        history_messages = snapshot.messages
        history_model_messages = tuple(
            ModelMessage(
                role="user" if m.role == "user" else ("assistant" if m.role == "assistant" else "system"),
                content=m.content,
            )
            for m in history_messages
        )
        assembled = (ModelMessage(role="system", content=system_content),) + history_model_messages
        self.last_assembled_messages = assembled

        provider = str(context.config_snapshot.get("provider", _DEFAULT_PROVIDER))
        if context.model_registry is None:
            yield HarnessEvent(
                kind="error",
                session_id=session_id,
                run_id=run_id,
                payload={"error": "model_registry missing"},
            )
            return
        client = await context.model_registry.get(provider)
        if client is None:
            yield HarnessEvent(
                kind="error",
                session_id=session_id,
                run_id=run_id,
                payload={"error": f"provider {provider!r} not registered"},
            )
            return

        model_request = ModelRequest(provider=provider, model=context.model, messages=assembled)
        usage = UsageSnapshot(model=context.model, provider=provider)
        text_accumulator: list[str] = []
        tool_outputs: list[str] = []

        try:
            async for event in client.stream(model_request):
                if event.kind == "text_delta":
                    delta = event.payload.get("text", "")
                    text_accumulator.append(delta)
                    yield HarnessEvent(
                        kind="model_delta",
                        session_id=session_id,
                        run_id=run_id,
                        payload={"text": delta},
                    )
                elif event.kind == "tool_call":
                    tool_name = event.payload.get("tool_name", "")
                    call_id = event.payload.get("call_id", _ANON_CALL_ID)
                    tool_input = event.payload.get("input", {}) or {}
                    if context.tool_registry is None:
                        yield HarnessEvent(
                            kind="error",
                            session_id=session_id,
                            run_id=run_id,
                            payload={"error": "tool_registry missing"},
                        )
                        return
                    tool = await context.tool_registry.get(tool_name)
                    if tool is None:
                        yield HarnessEvent(
                            kind="error",
                            session_id=session_id,
                            run_id=run_id,
                            payload={"error": f"tool {tool_name!r} not registered"},
                        )
                        return
                    try:
                        invocation = ToolInvocation(
                            tool_name=tool_name,
                            input=tool_input,
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
                        await tool.validate(invocation)
                        perm = await tool.check_permission(invocation, tool_ctx)
                        if perm.decision != "allow":
                            result = _denied_result(perm)
                        else:
                            prepared = await tool.pre_tool_use(invocation, tool_ctx)
                            result = await tool.execute(prepared, tool_ctx)
                            result = await tool.post_tool_use(prepared, tool_ctx, result)
                        rendered = await tool.render_result(result)
                    except Exception as tool_exc:
                        result = ToolResult(
                            ok=False,
                            content=None,
                            error=str(tool_exc),
                            failure_kind="execution",
                            rendered=f"[tool failed: {tool_exc}]",
                        )
                        rendered = result.rendered or ""
                    tool_outputs.append(rendered)
                    yield HarnessEvent(
                        kind="tool_finished",
                        session_id=session_id,
                        run_id=run_id,
                        payload={"text": rendered, "tool_name": tool_name, "ok": result.ok},
                    )
                elif event.kind == "usage" and event.usage is not None:
                    usage = replace(
                        usage,
                        exact_input_tokens=event.usage.input_tokens,
                        exact_output_tokens=event.usage.output_tokens,
                    )
                elif event.kind == "done":
                    break
                elif event.kind == "error" and event.error is not None:
                    yield HarnessEvent(
                        kind="error",
                        session_id=session_id,
                        run_id=run_id,
                        payload={"error": event.error.message},
                    )
                    return
        except Exception as exc:
            yield HarnessEvent(
                kind="error",
                session_id=session_id,
                run_id=run_id,
                payload={"error": str(exc)},
            )
            return

        final_text = "".join(text_accumulator)
        if tool_outputs:
            tool_section = "\n".join(tool_outputs)
            final_text = f"{final_text}\n{tool_section}" if final_text else tool_section
        assistant_msg = MemoryMessage(role="assistant", content=final_text)
        await context.active_context_store.append_message(session_id, agent_name, assistant_msg)
        await context.history_mirror.append(
            HistoryRow(
                row_id=f"{run_id}:assistant",
                session_id=session_id,
                agent_name=agent_name,
                kind="assistant",
                payload=assistant_msg,
                ts=0.0,
            )
        )
        if context.state_repository is not None:
            await context.state_repository.runs().update_usage(run_id, usage)
            await context.state_repository.runs().update_status(run_id, "completed")

        yield HarnessEvent(
            kind="final",
            session_id=session_id,
            run_id=run_id,
            payload={"text": final_text},
            usage=usage,
        )


def _default_permission(session_id: str) -> PermissionContext:
    return PermissionContext(mode="default", session_id=session_id)


def _denied_result(perm: PermissionResult) -> ToolResult:
    return ToolResult(
        ok=False,
        content=None,
        error=perm.reason or "permission denied",
        failure_kind="policy_forbidden",
        rendered=f"[denied: {perm.reason or 'no reason'}]",
    )
