---
title: agents
type: doc
status: active
---

# agents

## 定位

agent spec、outer API、会话侧行为包装。

## 详细设计

- [agents_spec](agents_spec.md) — Agent identity, model, tools, skills, permissions, memory scope.
- [agents_request_mapping](agents_request_mapping.md) — Map AgentRequest to HarnessRequest and HarnessContext.
- [agents_subagents](agents_subagents.md) — Subagent dispatch contract and delegation result shape.
- [agents_registry](agents_registry.md) — Agent registration and spec listing；含 `FileAgentRegistry`（两级目录 AGENT.md 热加载、删除）。

## 文件 Agent 文档（AGENT.md，计划 §5.1）

角色定义文档放在两级目录（同名时项目级覆盖用户级，内置 chat / researcher 不能被覆盖）：用户级 `{XANA_HOME}/agents/{name}/AGENT.md`（默认 `~/.xana/agents/`，所有项目通用）、项目级 `<project_root>/.xana/agents/{name}/AGENT.md`（本 serve 所属项目）。frontmatter 必填 `kind: agent` / `name` / `goal`，可选 `when_to_use`（何时用它，进可委派列表）/ `tools`（工具选择表达式：不写 = 项目 `agent_defaults.tools`（缺省 default 组合），`[]` = 零工具，可写组名 / 工具名 / `*`）/ `disallowed_tools` / `skills`（技能选择：不写 = 项目 `agent_defaults.skills`（缺省全部），`[]` = 无）/ `budget` / `stop_rules` / `output_contract` / `prompt_profile`（模式名或开关 mapping）/ `verify`（检查命令列表）；正文即系统提示词模板（含 `${base_prompt}` 在默认提示词上扩展，不含则完全替换），字段映射见 [agents_spec](agents_spec.md)，设计见 [agent-composition-design](../docs/design/agent-composition-design.md)。`FileAgentRegistry` 直接维护 `gateway.agents` 表并热加载：增、改、删 AGENT.md 不需要重启 serve（gateway 路由前、`agent_run`、可委派列表按 2 秒节流刷新，`/agent` 与 `agent_define` 立即刷新），builtin chat 保持默认（通用提示词，不带编码规则；写代码时加载 `coding` 技能，见 [harness_prompt_assembly](harness_prompt_assembly.md)）。设计见 [agent-delegation-design](../docs/design/agent-delegation-design.md) §7。

## 切换、委派与创建

设计见 [agent-delegation-design](../docs/design/agent-delegation-design.md)。

- **切换**：会话在创建时绑定 agent（`SessionConfig.agent`，`None` = 默认 chat），之后不切换。`/agent <名字>` 换一个新会话 id 并绑定该 agent，旧会话原样保留；`/clear` 把绑定带到新会话；router 据此决定 `target_agent`（见 [agent_adapter_commands](agent_adapter_commands.md)）。
- **委派**：`agent_run` 工具经 `AgentRunner` 在独立子会话里同步跑另一个 agent、取回最终文本，可并行、可 `resume` 续接；子 agent 拿不到 `agent_run`，深度固定为 1（见 [agents_subagents](agents_subagents.md)）。
- **创建 / 删除**：`agent_define` 工具（`catalog` 看可选工具组 / `create` 按 `scope` 写 `<project_root>/.xana/agents/<name>/AGENT.md`（`project`，缺省）或 `~/.xana/agents/<name>/AGENT.md`（`user`）/ `delete` 删除文件 agent）配合内置 `agent_creator` 技能（访谈 → 起草 → 用户在对话中确认 → create）；写入 / 删除后 `FileAgentRegistry.refresh(force=True)` 立即生效。新 agent 的工具不能超出调用方本 run 可用的工具，不能覆盖或删除内置 agent；两级 agent 目录对 Write / Edit 受保护，`agent_define` 是唯一写入口。
- **删除 / 修改对已有会话的影响**：绑定的 agent 被删除后，下一条消息 gateway 明确报错「agent X 不存在(可能已删除),用 /agent 选择其他 agent」，不静默改用 chat；被修改时新会话按新定义，已有会话的系统提示词在会话内固定，工具等其他字段从下一条消息起按新定义生效。

另有内置 `researcher`（`agents/researcher.py`）：deep_research 派出的子研究员，工具 `("web", "Read")`（web 组 + Read）、提示词 `${base_prompt}` + 研究员身份、max_turns 20、不向用户提问；配置了 xana-search 时由 boot 注册（不接进度汇报，不触碰渠道），不进文件 agent 注册表。

## 与 SKILL.md 的边界

AGENT.md 与 SKILL.md 共用同一个解析器（`MarkdownDocParser` 按 frontmatter `kind` 区分），但物理目录分开（`~/.xana/agents/` vs `~/.xana/skills/`），语义完全不同：**skill 是做法库**——prompt 指令包，经 `skill_load` 两阶段按需取全文，不 spawn 任何实体；**agent 是角色定义**——可被 spawn 的 AgentSpec 来源（系统提示词模板 + 工具选择 + 技能名单）。一个 agent 的 frontmatter `skills` 字段引用的是 skill 名（映射到 `AgentSpec.skills`，决定技能可见性：技能列表只列可见技能，`skill_load` 拒绝不可见技能）。详见 [skills](skills.md) 与 [skills_package](skills_package.md)。

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

Agent identity, system prompt template, allowed tools/skills, outer chat/control API, and session-facing behavior.

## Does Not Own

Harness turn loop, adapter protocol rendering, low-level LLM calls, or memory persistence format.

## Inputs

- `AgentRequest`
- `AgentControlRequest`
- session state
- harness SDK

## Outputs

- `AgentEvent` stream
- control results
- session updates through the configured store

## 独立 Agent 发布

发布分成两个独立部分：

- **能力包**：标准 `SKILL.md` 入口 + `xana-agent.json`（格式版本 1）+ `workspace/`。
  支持被官方 `npx skills` 发现和下载，附加清单声明入口 agent、依赖 agent、记忆/工作流/工具
  文件、按 agent 的技能绑定、Python 依赖、外部程序和业务配置项。文件用 SHA-256 校验。
- **运行核**：当前同一套 xana 实现的独立 wheel，带内容版本标识与依赖元数据；不重写执行链。
  发布 zip 同时带上运行核和纯标准库 `install.py`。仓库分发也可指定独立 wheel 的
  `runtime_url`（HTTPS）及摘要，接收方自动下载校验；未附带下载地址时要求包索引提供对应版本。

`agent_publish` 是专用发布工具，**仅内置默认 chat agent 可用**。`inspect` 查看选定文件和
发布阻塞项；`build` 生成发布 zip。`include` 必须明确选择项目内路径；`agents` / `tools`
可额外选择项目或用户目录中的依赖定义；已启用的全局技能连同 references 一起复制到包内，
仅恢复到原来绑定的 agent。原始项目不会被修改。

权限由 `ToolSpec.allowed_agents` 约束：harness schema/执行范围、委派清单、tool_search
与工具自身权限检查同时覆盖，子 agent 的上限排除 publishing 组。即使自建 agent 选择 `*`，
也不能获得发布工具。

发布检查拒绝已知凭据文件、硬编码凭据、本机外部绝对路径、符号链接和越界路径；报告只给
文件位置与原因，不输出凭据值。外部依赖必须明确纳入或改成接收方配置，检查器不会凭文本
猜测完整业务依赖，也不会静默替换另一种实现。`python_dependencies` 声明业务 Python 包，
`executables` 声明 ffmpeg 等程序，`settings` 声明需要询问接收方的业务环境变量。

接收方入口：

```bash
# 无需预装 xana：解压完整发布 zip 后（Python >=3.12，依赖包可从配置的包源下载）
python install.py

# 已有 xana 安装器时，安装体验类似 npx skills
xana agents add ./video-assistant.zip
xana agents add ./capability-repo --list
xana agents add https://github.com/owner/repo --list
xana agents add https://github.com/owner/repo --agent video-assistant
xana agents list
xana agents setup video-assistant
xana agents run video-assistant
```

安装位置是 `{XANA_HOME}/apps/<name>/`，入口位于 `{XANA_HOME}/bin/<name>`。
应用的 `state/` 自己保存模型、渠道、OAuth、会话、长期记忆与技能绑定；模型与飞书/TG
复用现有初始化、登录、配对流程。不会带走作者的机器人凭据、OAuth、聊天绑定或执行中的 run。
`setup` 可选 Kimi OAuth 或 API Key；业务秘密用隐藏输入，并以 0600 保存到应用私有配置。
`run` 在业务配置和外部程序齐备后，用现有 serve/boot 启动指定入口 agent。

应用首次初始化复制能力包的业务数据；之后重启保留接收方的记忆和工作状态。再次安装同名应用
或覆盖现有命令会明确拒绝，首版未做自动更新/迁移。发布只生成可分发文件，不自动推送 Git/PyPI
或向聊天对象发送文件。

实现：`publishing/contracts.py`（唯一清单契约）、`package.py`（检查/组包）、`artifacts.py`
（运行核 wheel）、`install.py`（接收安装）、`launcher.py` / `bootstrap.py`（初始化与启动）、
`cli/commands/agents.py`（安装 CLI）。测试：`tests/test_agent_publishing.py`。

发布应用使用当前内核的渠道实现；Telegram 原生图片/GIF/视频发送现已接入同一 `send_media`
链路，视频缩略图需 JPEG。TG 媒体适配器用 Bot API mock 验证，未向真实聊天发送测试消息。
初始化 Kimi 模型时，将服务端声明的 image/video 能力写入模型配置，保留用户明确关闭的设置；
运行时仍以配置为准。作者 agent 中不存在于接收方的固定模型名会在 setup 中提示映射，
只修改接收方工作副本。
