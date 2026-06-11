---
title: first_core_flow_v1_leftovers
type: todo
status: active
---

# first_core_flow_v1_leftovers — 并行接手前需要扫掉的 v1 已知遗留

**对应设计**: [docs/superpowers/specs/2026-06-11-first-core-flow-design.md](../docs/superpowers/specs/2026-06-11-first-core-flow-design.md)
**默认执行**: single_goal
**状态**: pending

## 背景

第一核心流 (`yuxuc → AgentGateway → ChatAgent → DefaultHarness → {Tool, Skill, Memory}`) 已经落地（125 个测试绿）。下面这些是 v1 显式接受的简化或测试态遗留，并行做 skill/tool/真 LLM 之前需要扫一遍，避免每个接手者各踩一次。

## 需要做什么

### L1 优先（并行开工前最好先做）

- [ ] **`DefaultHarness.last_assembled_messages` 是测试用 instance state**。`src/xana/harness_sdk/default.py` 在 `_invoke` 中把当回合 assembled `tuple[ModelMessage, ...]` 写到 `self.last_assembled_messages`，仅被 `tests/test_default_harness.py::test_invoke_collects_memory_injection_into_system_prompt` 读取。多 session 并发或 harness 复用时会被覆盖。改造方向：
  - 选项 A：emit `HarnessEvent(kind="turn_started", payload={"assembled": ...})`，测试改成消费事件。
  - 选项 B：在 `HarnessContext` 上挂一个可选 `prompt_capture: Callable[[tuple[ModelMessage, ...]], None] | None`，测试时传 capture 函数。
  - 现状：先用着，不要把它当成可读 API。

- [ ] **`MemoryMessage.token_estimate` 实际是字符数**。`src/xana/memory/active_context.py::InMemoryActiveContextStore.append_message` 用 `len(message.content)` 累加；字段名叫 "token estimate" 但单位是字符（英语约 4× 偏大）。任何下游 `MemoryPolicy.should_compact` 直接拿这个数算 token 预算会被误导。
  - 选项 A：替换成 `tiktoken` 或 provider 提供的 tokenizer（落 `memory_policy` 模块）。
  - 选项 B：临时改名 `char_estimate`，再加一个由真 model_client 回填的 `token_estimate`。
  - 暂时方案：维持字符数，但 compaction 触发器写出来时要明确读 `char_estimate`，别真当 token 用。

### L2 中等（接 real provider 时一起做）

- [ ] **`DefaultHarness` provider 静默 fallback 到 `"fake"`**。`src/xana/harness_sdk/default.py` 顶部常量 `_DEFAULT_PROVIDER = "fake"`，在 `_invoke` 里 `context.config_snapshot.get("provider", _DEFAULT_PROVIDER)`。一旦注册了 `"anthropic"` 真客户端而 `HarnessContext.config_snapshot` 配错，会无声路由到 fake，产生看似正常但完全错误的回复。
  - 接 real provider 那一刻改成：缺 `provider` 直接 emit `HarnessEvent(kind="error", payload={"error": "config_snapshot.provider missing"})` 并 return。
  - 与之配套：`ChatAgent` 的 `DefaultAgentRequestMapper` 必须始终填 `config_snapshot={"provider": self.provider}`，目前已经填好。

- [ ] **`CliAdapter.bind_session` 是空实现，gateway 也不会调它**。`DefaultAgentGateway.attach` 自己维护 `bindings: dict[str, SessionBinding]`，从不转发到 `adapter.bind_session`。
  - 当前实现：`cli.py::bind_session` 直接 `return None`，没有 dead state，但抽象方法形同虚设。
  - 加 Telegram/HTTP 等真 adapter 时，要决定：是 gateway 主动调 `adapter.bind_session(binding)`，还是 adapter 只在 `attach` 时由外部代码显式调用。统一一种再扩。

- [ ] **`subscribe(adapter_id, session_id)` 在 `DefaultAgentGateway` 里 `raise NotImplementedError`**。`src/xana/agent_adapter/gateway.py`。v1 CLI 是 push 模型（submit 直接拿到 stream），所以没实现。Telegram/HTTP 长连接 adapter 需要订阅模式时再补。

### L3 低（看 sprint 节奏）

- [ ] **FakeModelClient 串脚本（`ScriptedTurn`）的 `_cursor` 没暴露 reset**。多回合同一个 client 实例，最后一帧会卡在 last turn。`src/xana/llm_driver/fake_client.py`。
  - 真 provider 没这个问题；写多回合 e2e 测试时如果遇到，加 `def reset(self): self._cursor = 0`。

- [ ] **`runtime/boot.py` 里 `scripted_reply` 是 boot kwarg**。真 provider 接入后这个参数就没意义了，需要拆掉或挪到 `boot(provider="fake", fake_options={...})` 形式。先记着，接 anthropic 那次顺手拆。

- [ ] **`yuxuc` 输出会重复**：`model_delta` 事件 (mapped 成 `draft`) 被 `CliAdapter.render` 打印一次，`final` 事件携带累加文本又打印一次。
  - 修复方向：`CliAdapter` 看到 `event_kind == "draft"` 时直接写 delta（不换行、不 flush 句末），收到 `final` 时只打换行不重打文本。
  - 或者 `DefaultAgentEventMapper` 把 `final` 的 payload `text` 去掉，让 adapter 只靠 deltas。
  - 现状：125 个测试不在乎这个；用户体验不行但不阻塞并行。

## 关键代码锚点

- `src/xana/harness_sdk/default.py` — `_invoke`、`last_assembled_messages`、`_DEFAULT_PROVIDER`
- `src/xana/memory/active_context.py` — `InMemoryActiveContextStore.append_message` 的 `token_estimate`
- `src/xana/agent_adapter/cli.py` — `bind_session`、render 的 delta+final 双打
- `src/xana/agent_adapter/gateway.py` — `subscribe`、`attach` vs `adapter.bind_session`
- `src/xana/llm_driver/fake_client.py` — `_cursor` 无 reset
- `src/xana/runtime/boot.py` — `scripted_reply` kwarg

## 约束 / 不变量

- 任何修复不得改 `src/xana/*/interfaces.py` 或 `src/xana/*/contracts.py`。要改先开新设计提案。
- 单测必须先红再绿；canonical `tests/test_fake_pipeline_flow.py` 不许动。
- 状态权威：ActiveContextStore 拥有 runtime context，HistoryMirror 是 mirror，RunStore 拥有 run 元数据。补丁不许破坏这条。

## 不做

- 接入真 LLM 提供商（独立任务）。
- 实现 sandbox / hook runner / subagent dispatcher（不在第一核心流目标里）。
- 持久化 SessionStore / RunStore（依然 in-memory 即可）。

## 测试要点

- [ ] 任一 L1 / L2 修复都要补对应单测（断言新行为），不只是把旧测试改适配。
- [ ] 修 provider fallback 时加一个 "no provider in config_snapshot → error event" 的单测。
- [ ] 修 CLI 重复输出时加 e2e 断言：scripted reply 在 stdout 里只出现一次。

## 收工

- [ ] 跑全量 `python -m pytest tests/`
- [ ] 跑 yuxuc 烟测 `PYTHONPATH=src python -m xana.cli.yuxuc "hello"`
- [ ] 把已完成项从本文件里删掉；全部清空时把这个 TODO 文件归档
- [ ] 若改动影响 architecture，先更新 `doc_ai` 当前真相
