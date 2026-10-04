---
title: agents spec
type: doc
status: active
---

# agents spec

## 上级模块

- [agents](agents.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/agents/contracts.py) — `AgentSpec` 及相关枚举
- [registry.py](../src/xana/agents/registry.py) — `agent_spec_from_doc()`：AGENT.md manifest+正文 → `AgentSpec` 字段映射
- [system_prompt.py](../src/xana/harness_sdk/system_prompt.py) — `build_system_prompt`，`system_prompt` 模板的实际消费方
- [toolsets.py](../src/xana/tools/toolsets.py) — 内置工具组表与 `resolve_tools`（`tools` 表达式的解析方）
- 设计：[agent-composition-design](../docs/design/agent-composition-design.md)

## 职责

`AgentSpec` 是 agent 的声明式配置（frozen dataclass）：身份、prompt、工具/技能/权限/沙箱约束、记忆范围、隔离与后台行为。它本身不执行任何逻辑，由 [ChatAgent](agents_registry.md) 和 [DefaultAgentRequestMapper](agents_request_mapping.md) 在运行时消费。

## 字段语义

- `component: ComponentSpec` — agent 身份载体（kind/name/qualified_name/source），`name` 是 gateway 路由和 harness `agent_name` 的实际值。
- `prompt_id: str` — prompt 模板标识（如 builtin chat 用 `"chat-default"`）；v1 未做模板查表，实际 prompt 由 harness 13 段拼装。
- `when_to_use: str` — 描述该 agent 适用场景；AGENT.md 同名字段映射而来，拼进 `agent_run` 的可委派 agent 列表供模型挑选。
- `tools: tuple[str, ...] = ("default",)` — 工具选择表达式，元素为 `default`（所有默认组的工具）、组名（见 [tools_spec](tools_spec.md) 的工具组）、工具名或 `*`（工具目录全部）；`()` = 零工具。builtin chat 与没写 `tools` 的 AGENT.md 取项目 `agent_defaults.tools`（缺省 `["default"]`）。
- `disallowed_tools: tuple[str, ...]` — 在 `tools` 结果上再减，同样接受组名。两者由 `ChatAgent` 放进 `config_snapshot`（键 `tools` / `disallowed_tools`），harness 每次 run 用 `resolve_tools` 解析一次，tools schema、执行前校验与各项注入都以结果为准（见 [harness_tool_dispatch](harness_tool_dispatch.md)）。写错的组名 / 工具名 boot 记 warning。
- `skills: tuple[str, ...] | None = None` — 技能选择（取代原 `allowed_skills`，[project-design](../docs/design/project-design.md) §8）：`None` = 全部可见，`()` = 没有技能，名单 = 只这些，含 `*` = 全部。`ChatAgent` 放进 `config_snapshot["skills"]`（`None` 或含 `*` → `None`，否则排序名单），harness 转成 `TurnInputContext.visible_skills` / `ToolContext.visible_skills`：技能列表只列可见技能，`skill_load` 拒绝不可见技能（见 [skills](skills.md)）。builtin chat 与没写 `skills` 的 AGENT.md 取项目 `agent_defaults.skills`。
- `hooks: tuple[str, ...]` — 钩子名单，仍仅声明。
- `model: str | None` / `effort: str | int | None` — 默认模型与推理强度；运行时以 `SessionConfigStore` 的会话配置优先（见 [agents_request_mapping](agents_request_mapping.md)）。
- `permission_policy: str | None` / `sandbox_policy: SandboxPolicySpec | None` — 指向 [policy](policy.md) 模块的合约，v1 未执行。
- `memory_scope: MemoryScope | None` — `user / project / agent / session` 之一。
- `isolation: IsolationMode`（`none / worktree / process / remote`）、`background: bool` — 声明执行形态，v1 未执行。
- `parent_agent: str | None` — 父 agent 名，供 subagent 委派链使用（见 [agents_subagents](agents_subagents.md)）。
- `max_turns: int | None` — 单次 run 的工具轮上限，经 `config_snapshot["max_turns"]` 传入 harness；`None` = 不限（与 CC 交互模式、Kimi Code 一致）；显式配置时用完后有一轮收尾总结。
- `system_prompt: str | None` — `None` → xana 默认系统提示词；字符串 → 系统提示词模板：`${base_prompt}` 嵌入完整默认提示词（在默认上扩展），`${env}` 环境段，`${extensions}` 扩展片段，未知占位符原样保留；不含占位符 = 完全替换（见 [harness_prompt_assembly](harness_prompt_assembly.md)）。
- `prompt_profile: PromptProfile | None` — 会话级提示词开关（`harness_sdk/contracts.py`）。`None` = 交互默认。会话首次使用时由 ChatAgent 固化进 `SessionConfig.prompt_profile`，之后改 AGENT.md 不影响已有会话。两种模式默认值（照 Claude Code，少干预）：
  - `interactive`：所有开关关闭，system 不额外加任何文本；
  - `unattended`：只加 CC 自主运行段（随模式）+ `goal_mode=True`（目标评估器）；
  - 可按 agent 自愿开启：`standing_order`（无人值守长期指令）、`grounded_progress`（长版进度核查）、`answered_as_done`、`explore_first`、`time_signal`（`elapsed` 在工具结果末尾追加"已用 N 秒"）。
- `verify: tuple[VerifyCheck, ...]` — 回合结束时的检查命令（`cmd`，`timeout_s` 默认 300）。模型以纯文本结束一轮时在工作目录依次执行，非 0 / 超时即以输出为反馈续跑（见 [harness_turn_controller](harness_turn_controller.md)）。只能由 agent 作者声明。

## AGENT.md 文档 → AgentSpec 映射（计划 §5.1）

角色定义文档放在用户级 `{XANA_HOME}/agents/{name}/AGENT.md`（默认 `~/.xana/agents/`，`XANA_HOME` 环境变量可覆盖）或项目级 `<project_root>/.xana/agents/{name}/AGENT.md`（同名时项目级优先），由 [FileAgentRegistry](agents_registry.md) 经共享解析器 [MarkdownDocParser](../src/xana/skills/markdown_loader.py)（frontmatter `kind: agent`）加载，`agent_spec_from_doc()` 做字段映射：

| AGENT.md frontmatter | AgentSpec 字段 | 说明 |
|---|---|---|
| `kind: agent`（必填） | — | 区分文档 kind；缺省视为 skill |
| `name`（必填） | `component.name` | 同名校验非空 |
| `goal`（必填） | `component.description` | goal 映射到 manifest.description |
| `when_to_use`（可选，字符串） | `when_to_use` | 何时用它；进可委派 agent 列表（见 [agents_subagents](agents_subagents.md)） |
| `tools`（可选，字符串列表） | `tools` | 工具选择表达式；不写 = 项目 `agent_defaults.tools`（缺省 `("default",)`），`[]` = 零工具 |
| `disallowed_tools`（可选，字符串列表） | `disallowed_tools` | 在 `tools` 结果上再减，接受组名；没写 `tools` 时并入项目 `agent_defaults.disallowed_tools` |
| `skills`（可选，字符串列表） | `skills` | 技能选择；不写 = 项目 `agent_defaults.skills`（缺省 `None` = 全部），`[]` = 没有技能 |
| `budget`（可选，mapping） | `component.metadata["budget"]` | AgentSpec 无对应字段 |
| `stop_rules`（可选，字符串列表） | `component.metadata["stop_rules"]` | 阶段四消费 |
| `output_contract`（可选，字符串） | `component.metadata["output_contract"]` | 阶段四消费 |
| 正文（frontmatter 之后） | `system_prompt` | 系统提示词模板；空正文 = `None`（默认提示词） |

正文含 `${base_prompt}` 时在默认提示词上扩展，不含则完全替换。生成的其他字段：`qualified_name="user.{name}"`、`prompt_id="agent.{name}"`、`ComponentSource(source_type=scope, scope=scope, path=AGENT.md 路径)`（scope 为所在目录级别 `user` / `project`）。

不写 `tools` / `skills` 即「跟随项目默认」：项目 `agent_defaults`（项目 config.json，见 [config](config.md)）改了，这些 agent 随之改变；`agent_define` 新建 agent 不填 tools / skills 同理。

`tools` / `disallowed_tools` 由 harness 强制执行（schema 只含解析结果内的工具，范围外调用返回 policy_forbidden）；`skills` 同样「可见 + 执行前校验」：列表只列可见技能，`skill_load` 对不可见技能返回 `not_found`。

## 不变量

- Spec 为 frozen 值对象，运行期不可变；会话级覆盖走 `SessionConfigStore`，不改 spec。
- `system_prompt` / `tools` / `disallowed_tools` 经 `DefaultAgentRequestMapper` 放入 `HarnessContext.config_snapshot`（键 `agent_system_prompt` / `tools` / `disallowed_tools`），由 harness 消费，adapter 不可绕过。
