---
title: skills runtime
type: doc
status: active
---

# skills runtime

## 上级模块

- [skills](skills.md) — 模块设计

## 代码锚点

- [interfaces.py](../src/xana/skills/interfaces.py) — `Skill`, `SkillRegistry`
- [registry.py](../src/xana/skills/registry.py) — `InMemorySkillRegistry`
- [bundled/](../src/xana/skills/bundled/__init__.py) — `loop` / `schedule` / `skill_creator` 三 skill
- [markdown_loader.py](../src/xana/skills/markdown_loader.py) — `MarkdownSkill` / `FileSkillLoader` / `scan_skill_docs()`，文件 SKILL.md 的加载
- [boot.py](../src/xana/runtime/boot.py) — bundled + 文件 skill registry 组装 + `SkillLoadTool` 接线
- [skills_listing.py](../src/xana/extension_api/builtin/skills_listing.py) — `SkillsListingContributor`

## 职责

技能的运行时：Skill 抽象、内存注册表、bundled 技能集，以及两阶段注入的组装。

## Skill / SkillRegistry

- `Skill` ABC 只有两个实质方法：`spec() → SkillSpec`、`activate(activation) → SkillContribution`；外加 `deactivate(session_id)`（会话粒度反激活，bundled 实现为 no-op）。
- `InMemorySkillRegistry`：dict 按键 `spec().component.name`，`register` 同名覆盖；`get(name)` 按名取；`discover(activation)` v1 直接返回**全部**已注册技能（注释：未来可按 activation_paths/cwd/kind/agent 过滤）。进程内、非持久化。

## bundled skill

`src/xana/skills/bundled/` 下的内置技能，全文为模块级内嵌字符串 `_BODY`（前三个是 CC 技能的中文忠实改写）：

| 技能 | 文件 | 说明 |
|---|---|---|
| `loop` | `loop.py` | 周期性执行一个 prompt：解析间隔（10m/2h/1d 等）→ 换算 cron → 调 `cron_create` + 立即执行一次 |
| `schedule` | `schedule.py` | 管理本地定时任务：意图澄清 → 定时间 → 确认 → `cron_create`/`cron_list`/`cron_delete` |
| `skill_creator` | `skill_creator.py` | 把会话流程沉淀为 SKILL.md（分析 → 访谈 → 成稿确认；访谈降级为普通提问，因无 request_user_input 工具） |
| `agent_creator` | `agent_creator.py` | 访谈 → 起草 → 用户确认 → `agent_define create`（见 [agents](agents.md)） |
| `project_creator` | `project_creator.py` | **只在 default 项目注册**：弄清用途 → 起名 → 选工具 / 技能默认可见性 → 确认 → `project_manage create` → `bind_feishu` → `feishu_status` → `start`（[project-design](../docs/design/project-design.md) §7.2） |

各技能 `activate()` 都返回 `SkillContribution(prompt_blocks=(_BODY,))`，无状态。

## 两阶段注入模式

boot 里有两个独立的 `InMemorySkillRegistry`：

1. **harness 的 `skill_registry`**（`ChatAgent.skill_registry`）：boot 时是**空的**，无任何注册——这是**有意设计**：文件 skill 走与 bundled 相同的两阶段按需加载（列表注入 + `skill_load` 取全文），不做每轮全文注入，所以不需要一个每轮注入用的 registry。
2. **bundled + 文件 skill registry**（boot 局部变量 `bundled_skills`）：注册 bundled skill（default 项目另加 project_creator），以及 `scan_skill_docs()` 从 `{XANA_HOME}/skills/*/SKILL.md` 加载的文件 skill（`XANA_HOME` 默认 `~/.xana`）。文件 skill 与 bundled 是**同一个 registry、同一个 `Skill` 接口**（`MarkdownSkill.activate()` 同样返回正文 `prompt_blocks`），被两处消费：
   - `SkillsListingContributor(skills=bundled_skills)`：每轮向 system prompt 注入「# 可用技能」段——每技能一行 `名字: 描述`，**不含全文**（零常驻 token），文件 skill 自动出现在列表里；agent 没有 `skill_load` 时不列，只列 `TurnInputContext.visible_skills` 内的技能（None = 全部）。仅当调用方未传 `extension_registry` 时由 boot 自动挂上。
   - `SkillLoadTool(skills=bundled_skills)`：模型看到列表后按需调 `skill_load` 工具取全文，工具内部 `get(name)` → `activate()` → join `prompt_blocks` 返回，对文件 skill 和 bundled skill 一视同仁；名字不在 `ToolContext.visible_skills` 内时按不存在处理（`not_found`）。

注意 `skill_load` 注入的是 bundled registry 而非 harness 的 `skill_registry`——两阶段模式的全部闭环都在这一个 registry 上，harness 那个口保持空转是刻意的（不做每轮全文注入）。坏 SKILL.md 由 `scan_skill_docs()` 收集为错误，boot 记 warning 跳过，不阻塞启动（见 [runtime_boot](runtime_boot.md)）。

## 与 AGENT.md 的边界

`MarkdownDocParser` 是 SKILL.md 与 AGENT.md 共用的解析器，但按 frontmatter `kind` 分流：`kind: skill`（默认）走本文的两阶段加载；`kind: agent` 由 [FileAgentRegistry](../src/xana/agents/registry.py) 消费，产出可 spawn 的 AgentSpec（见 [agents_registry](agents_registry.md)）。`FileSkillLoader` 遇到 kind=agent 的文档会抛 `SkillManifestError`。skill 是做法库，agent 是角色定义。
