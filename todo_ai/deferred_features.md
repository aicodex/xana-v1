# Tools/Skills 待办(依赖未完成功能,2026-06-13 计划遗留)

来源:ds_review/xana_review/tools-skills-checklist.md;已完成项见
docs/superpowers/plans/2026-06-13-tools-skills.md。

## 等待前置功能的项

- **request_user_input 工具**(CC AskUserQuestionTool):需要 adapter 层支持
  选项卡片渲染 + 回传选择(feishu/tg 交互消息)。做好后:skill_creator 的访谈
  改用本工具;schedule skill 的「创建/查看/修改/删除」首问也改用。
- **batch skill**(CC batch.ts):需要 worktree 隔离的后台子代理(AgentSpawner
  实现 + isolation="worktree")与 PR 工作流。前置:AgentSpawner 具体实现。
- **permission_check 系统级能力**:ToolLifecycleContributor 注入执行前权限
  检查,需要 runtime policy engine(政策规则来源 + 审批 UI)。
- **sandbox_run 系统级能力**:SandboxManager 具体实现(xana 无 sandbox,从零建,
  参考 Codex sandboxing/)。
- **schedule skill 的 remote 形态**(CC scheduleRemoteAgents):需要云端执行
  基础设施;当前为本地 cron 适配版(skills/bundled/schedule.py)。
- **web_fetch 的小模型加工**(CC 行为:prompt 参数 + Haiku 提炼):需要工具内
  二次 LLM 调用通道(构造注入 model_registry);当前返回抽取正文,由主模型自读。
- **compact 保留 revealed tools**:CC 在 compact boundary 记
  preCompactDiscoveredTools;xana 当前 compact 后 revealed 状态丢失,模型需
  重新 tool_search。做法:build_post_compact_context 透传 revealed 名单进
  boundary metadata,_revealed_tools 兼容读取。
- **boot 外部传入 extension_registry 时的「# 可用技能」段**:builder frozen,
  boot 无法向调用方传入的 registry 追加 SkillsListingContributor;需要 builder
  合并能力,或调用方自行 add(SkillsListingContributor(...))。
- **enter_plan_mode / enter_worktree 转 deferred**:机制已就绪(ToolSpec.defer),
  仅差一行 spec 改动;待观察 schema 体积是否值得隐藏后再切。
