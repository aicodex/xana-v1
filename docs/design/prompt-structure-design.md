---
title: 提示词结构:通用核心 + 编码技能 + 项目说明 AGENTS.md
status: active
date: 2026-09-29
---

# 提示词结构

## 1. 现状问题(2026-09-29 对线上 chat 第一轮请求的审查)

- 系统提示词 4.5K 字,全部译自 Claude Code;身份是"交互式编码助手",其中约 1700 字是写代码的规则(snake_case、注释、dev server、OWASP、`owner/repo#123`),默认项目的聊天(投研、新闻)用不上。
- 几句写的是 xana 没有的机制:交互式权限审批、钩子(hooks 模块已删除)、"等宽字体 CommonMark 渲染"(飞书卡片不是)、`/help` 与 issue 反馈。
- 缺项目说明文件:CC 读 `CLAUDE.md`,Codex / Kimi 读 `AGENTS.md`,xana 没有(代码里留着 TODO)。
- "融合三家"没做到:Codex 与 Kimi 的长处没有进来。

## 2. 设计

### 2.1 通用核心(所有默认提示词的 agent 共享)

| 段 | 内容 | 来源 |
|---|---|---|
| 开场 | 中性身份:"你是 xana,通过聊天渠道和命令行为用户工作的助手";不编造 URL(只用用户给的、文件里的或工具返回的链接) | CC 改写 |
| 系统 | 输出即对用户说话;按渠道渲染 Markdown(用常见语法,不依赖表格嵌套 / 等宽排版);被拒绝的调用不原样重试;`<system-reminder>`;prompt injection;自动压缩 | CC,删不存在的机制 |
| 做事 | 探索性问题给推荐与权衡;失败先诊断;不做超出要求的事 | CC「执行任务」的通用部分 |
| 谨慎执行 | 可逆性与影响半径、风险操作先确认(例子去掉 git 专有项)、不确定就可逆处理、如实报告 | CC,git 细节移入编码段 |
| 环境 | 目录、平台、模型(日期以 note 注入) | CC |
| 使用工具 | 优先专用工具;todo;并行调用 | CC + Kimi 的并行提示 |
| 沟通 / 输出效率 | 写给追进度的队友;最终消息自足;简洁 | CC 2.1.281 |
| 回答格式 | 结论先行;结构只在有助浏览时用;列表不嵌套、每条一行、按重要性排序;简单问题直接用句子 | Codex「Final answer structure」 |

### 2.2 编码:`coding` 技能

- 写代码的规则(snake_case 例子、优先改现有文件、安全漏洞、不过度抽象 / 不加多余错误处理与兼容层、注释规范、UI 要实际跑过、`file_path:行号` 与 `owner/repo#123`、git 操作前先 status / stash、安全测试政策)做成内置技能 `coding`(`skills/bundled/coding.py`)。
- 技能列表里的描述写明"写代码、改代码、调试、跑测试或做 git 操作之前先加载";模型 `skill_load` 后规则进入上下文。default 项目的 AGENTS.md 也提示这一点。
- 不做成工具:规则是给模型读的说明,没有要执行的动作。不另设编码 agent(只留一个入口);以后编码单独成项目时,在那个项目的 agent 模板里放进这段规则即可。

### 2.3 项目说明:项目根目录的 `AGENTS.md`

- 会话开始时读项目目录(`project_root`)下的 `AGENTS.md`,作为 note 追加在第一条用户消息后(内容变化才重新注入);没有文件不注入;超过 20K 字截断并注明。
- 措辞译自 CC 的 CLAUDE.md 注入:"以下是本项目的说明(项目根目录 AGENTS.md),请遵循;它们优先于默认行为。"
- 与环境段同一条件:完全替换提示词且不放 `${env}` / `${base_prompt}` 的 agent 不注入。
- default 项目创建时写入 `AGENTS.md`(`runtime/projects.DEFAULT_INSTRUCTIONS`):默认项目的定位,写代码前先加载 coding 技能。已有文件不覆盖。`project_manage create` 增加可选 `instructions`,由 project_creator 访谈后写入新项目的 AGENTS.md。

### 2.4 子 agent 的交付说明(Kimi)

`GatewayAgentRunner` 在子会话的第一条任务前加一段说明(译自 Kimi Code `profile/explore-overlay.md`;resume 不重复):你在完成主 agent 派出的子任务;主 agent 看不到你的过程,只能看到你的最后一条消息;不要向用户提问;最后一条消息要完整交付结论与依据。

### 2.5 小修

- `Bash` 描述、chat 的说明改为中文;`tool_search` 描述与实际注入的按需工具提示对齐。
- 可委派列表中,工具与调用方上限相同的 agent 显示"与你相同(不含委派类)",不再逐个列出。

## 3. 影响

- 系统提示词会话内固定,只影响新会话;默认 chat 的系统提示词 4.5K → 2.9K 字。
- 已安装的 default 项目不会自动补 `AGENTS.md`(ensure_default_project 只在创建时写),部署时手动写一次。
- 编码规则不删除,移入 `coding` 技能。

## 4. 验证

- 抓取线上配置下 chat 的第一轮请求,核对结构与字数;chat 不含编码段,技能列表有 coding。
- AGENTS.md:有 / 无 / 变化 / 超长 / 纯净 agent 不注入。
- 子 agent 任务前缀;全量测试 + compact 回归网。
