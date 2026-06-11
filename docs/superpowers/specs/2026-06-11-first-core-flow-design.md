---
title: xana First Core Flow Design
date: 2026-06-11
status: approved
---

# xana First Core Flow Design

## Goal

Land the first end-to-end vertical slice of xana:

```
xanac (CLI) → AgentGateway → ChatAgent → Harness → {Tool, Skill} ← Memory
```

Every link must implement the abstract interfaces in `src/xana/*/interfaces.py` as-is. No interface changes. This slice is the reference that future parallel work (more tools, more skills, real LLM driver, more adapters) will plug into.

## Non-Goals

- Real LLM provider call. `FakeModelClient` only.
- Interactive permission approval. Silent allow for v1.
- Sandbox enforcement.
- Subagent dispatch.
- Resume from checkpoint / persistent state.
- Hook runner execution (interface stays, registry empty).
- Telegram / Feishu / HTTP adapter (CLI only).

## Architecture

### Contract surface (frozen, do not edit)

All interfaces in `src/xana/*/interfaces.py` are taken as fixed. Implementations must satisfy:

- `Adapter`, `AgentGateway`, `AgentRequestRouter`, `DraftRenderer`
- `Agent`, `AgentRequestMapper`, `AgentEventMapper`
- `Harness` (full orchestration loop in one place)
- `Tool`, `ToolRegistry`
- `Skill`, `SkillRegistry`
- `ActiveContextStore`, `HistoryMirror`
- `MemoryTree`, `MemorySearchEngine`, `MemoryInjector`, `MemoryInjectorRegistry`
- `ModelClient`, `ModelRegistry`

### Message flow (per turn)

1. User types `xanac "hello"` (or runs interactive REPL).
2. `CliAdapter.receive()` async-yields one `InboundMessage`.
3. `DefaultAgentGateway.submit(inbound)` routes via `DefaultAgentRequestRouter` to `AgentRequest(target_agent="chat")`.
4. `ChatAgent.send(request)` calls its mapper → `(HarnessRequest, HarnessContext)`. Context carries the shared registries/stores (not freshly minted per turn — wired once at boot, mapper passes the references through).
5. `DefaultHarness.invoke(request, context)`:
   - `ActiveContextStore.append_message` for the user turn.
   - `HistoryMirror.append` for the user row.
   - `MemoryInjectorRegistry.collect(...)` → `tuple[InjectedBlock, ...]` placed as system-head prompt blocks.
   - `SkillRegistry.discover(SkillActivation(kind="model", ...))` → `Skill.activate` → flatten `prompt_blocks`.
   - Assemble `ModelRequest(messages=(system=injected+skill, user=user_input))`.
   - `ModelClient.stream(...)` — on `tool_call`, run full `Tool` lifecycle (`check_permission` → `pre_tool_use` → `execute` → `post_tool_use` → `render_result`) and emit `HarnessEvent(kind="tool_finished")`. On `usage`, accumulate into `UsageSnapshot`. On `done`, append assistant message + emit `HarnessEvent(kind="final")`.
6. `ChatAgent`'s `AgentEventMapper`: `kind=="final" → "final"`, else `"draft"`.
7. `CliAdapter.render(agent_event)` writes to stdout. Final event also signals an `asyncio.Event` so the CLI can exit.

### Memory wiring

- `MarkdownMemoryTree(root=cwd, entrypoint="README_ai.md")` loads docs at boot.
- `SQLiteMemorySearchEngine` (in-memory db) is rebuilt from the tree.
- `SearchMemoryInjector` wraps the engine and is registered with priority 100 in `MemoryInjectorRegistry`.
- `DefaultHarness` calls `collect()` and places blocks per their `placement` field.
- The `memory_search` Tool also calls the same `MemorySearchEngine`, so model-side L2 query and prompt-side L1 injection share one substrate.

### CLI wiring (`xanac`)

- Console script `xanac` → `xana.cli.xanac:main`.
- `argparse`: `xanac TEXT...` (one-shot) or `xanac` (REPL).
- Flags: `--project DIR` (default cwd), `--session ID`.
- `runtime.boot.boot(project_root)` returns a wired `AgentGateway` plus references to underlying stores/registries (so tests can introspect).
- Adapter writes deltas inline to stdout; on final, prints a newline and sets the turn-complete event.

## File layout (new)

```
src/xana/
  agent_adapter/
    cli.py            # CliAdapter, StdoutDraftRenderer
    gateway.py        # DefaultAgentGateway
    router.py         # DefaultAgentRequestRouter
  agents/
    chat_agent.py     # ChatAgent + DefaultAgentRequestMapper + DefaultAgentEventMapper
  harness_sdk/
    default.py        # DefaultHarness
    turn_controller.py # InMemoryTurnController + InMemoryRunStore
  tools/
    registry.py       # InMemoryToolRegistry
    builtin/
      __init__.py
      memory_search.py # MemorySearchTool
  skills/
    registry.py       # InMemorySkillRegistry
    builtin/
      __init__.py
      identity.py     # IdentitySkill (injects xana identity system block)
  memory/
    active_context.py # InMemoryActiveContextStore + InMemoryHistoryMirror + DefaultMemoryInjectorRegistry
  llm_driver/
    fake_client.py    # FakeModelClient + InMemoryModelRegistry
  runtime/
    boot.py           # boot(project_root) -> BootResult
  cli/
    __init__.py
    xanac.py          # main() entry point
tests/
  test_xanac_e2e.py   # subprocess CLI smoke
  test_default_harness.py # DefaultHarness against the test_fake_pipeline_flow contract
```

## Error model

- Tool execution exceptions → `ToolResult(ok=False, failure_kind="execution", error=...)` then `HarnessEvent(kind="tool_finished")` with the failed result.
- Model stream raising → `HarnessEvent(kind="error", payload={"error": str})` then close.
- CLI: exit code 0 on `final`, non-zero on `error`.

## Replacement / extensibility

Each new file ships with a clear surface so future parallel work can swap in:

- Real `ModelClient` (Anthropic) plugs into `ModelRegistry.register("anthropic", ...)`.
- New `Tool` impls register via `ToolRegistry.register(...)`.
- New `Skill` impls register via `SkillRegistry.register(...)`.
- New `Adapter` (Telegram, HTTP) registers via `AgentGateway.register_adapter(...)`.

## Memory mount

`README_ai.md` is unchanged in structure but gains:
- A "Runtime entry" pointer to `cli/xanac.py`.
- A "First core flow" status note under "项目入口".

## Acceptance criteria

1. `pytest tests/` green, including the existing `test_fake_pipeline_flow.py`, `test_interfaces_are_abstract.py`, etc.
2. New `test_default_harness.py` exercises `DefaultHarness` end-to-end with the same assertions shape as the canonical test.
3. New `test_xanac_e2e.py` shells out to `xanac "hello"` and asserts stdout contains the expected `harness-final:` envelope.
4. `MemoryInjectorRegistry.collect()` is exercised during a real turn (verified via an assertion that an injected block from `README_ai.md` made it into the model's system prompt).
5. `git init` done, remote `git@github.com:aicodex/xana.git` set, two local commits, no push.
