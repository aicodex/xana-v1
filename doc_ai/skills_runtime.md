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
- [registry.py](../src/xana/skills/registry.py) — `InMemorySkillRegistry` / `ManagedSkillRegistry`
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

boot 使用同一个 `ManagedSkillRegistry` 保存内置技能，并通过 `SkillManagement`
查询明确启用的外部技能。`SkillsListingContributor` 与 `SkillLoadTool` 都按
当前 agent 调用 `discover`，`AgentSpec.skills` 仍作为进一步收窄的名单。
内置技能保持原有行为；文件技能不再因为出现在磁盘上而自动对所有 agent 开放。

## 安装与按 agent 启用

`skill_manage` 是用户聊天中的管理工具，子 agent 的委派上限排除此工具。

- `list`：查看本地技能及已启用的 agent；带 `source` 列远程来源中的技能。
- `install`：先按技能名查本地，已有则复用；缺少时通过官方 `npx skills` 安装。
  `skills=["*"]` 明确安装来源中的全部技能。安装先落到临时目录，校验完整包后
  归入项目 `.agents/skills/`，保留 references 等资源；已有包不覆盖。
- `enable` / `disable`：必须显式给出 `skills` 和 `agents`，只调整指定 agent。
- `install` 可同时给 `agents`，完成本地就绪检查后再启用；不填只安装、不改变启用范围。

本地库存按 `~/.agents/skills` → `{XANA_HOME}/skills` → 项目 `.agents/skills` 的优先级
查询，同名项目包优先。库存与启用范围分开：目录只负责包内容，项目
`skill_bindings.json` 是外部技能启用范围的唯一记录，按 agent 保存名字和已选包路径。
启用记录原子写入，重启后有效；启用无需重启。包缺失不会退到另一份同名实现。

例如：

```json
{"action":"install","source":"https://github.com/MiniMax-AI/MiniMax-H3",
 "skills":["h3-prompt-writing"],"agents":["video-director"]}
```

官方 npx CLI 由 `ProcessRunner` 以 argv 调用，有超时、取消支持；缺少 npx 或安装失败
明确报错。通用 `npx` 命令仍可经 Bash 执行，但直接往标准目录放文件不会获得启用权限。
安装不执行技能包的脚本，不自动提供技能需要的第三方工具/API/凭据。
`skill_load` 返回真实技能目录，资源相对路径以该目录为准。

验证：`tests/test_skill_install_loading.py`（本地复用、不联网、只给指定 agent、拒绝猜名字
加载、名单继续收窄、重启保留、禁用不删包、参考文件保留及覆盖保护）。

## 与 AGENT.md 的边界

`MarkdownDocParser` 是 SKILL.md 与 AGENT.md 共用的解析器，但按 frontmatter `kind` 分流：`kind: skill`（默认）走本文的两阶段加载；`kind: agent` 由 [FileAgentRegistry](../src/xana/agents/registry.py) 消费，产出可 spawn 的 AgentSpec（见 [agents_registry](agents_registry.md)）。`FileSkillLoader` 遇到 kind=agent 的文档会抛 `SkillManifestError`。skill 是做法库，agent 是角色定义。
