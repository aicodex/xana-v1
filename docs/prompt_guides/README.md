# 提示词指南调研（中文）

2026-09-23 整理。全部为中文，不保留英文原文；每项按"用途 / 做法 / 效果 / 可能遇到的问题 / xana 落地建议"组织。

| 文件 | 内容 |
|---|---|
| [anthropic_opus55.md](anthropic_opus55.md) | 用户提供的 Opus 5.5 官方做法：回合续跑、无人值守指令、进度更新、动手前广泛查看、时间预算、聊天思考控制、粘贴内容防注入、视觉、前端风格 |
| [anthropic_claude.md](anthropic_claude.md) | Claude Fable 5.1 / Opus 5 / Opus 5.5 / Sonnet 5 官方文档调研：历史只追加、会话中途系统消息、effort 与缓存、task_budget、子 agent 非阻塞等 |
| [openai_gpt.md](openai_gpt.md) | GPT-5.6 官方指南 + GPT-6（Astra/Sol/Luna）最佳实践与迁移：结果导向短系统提示、冲突规则、主动性分级、phase 字段等 |
| [kimi.md](kimi.md) | Kimi（K2.8 / K3 / kimi-for-coding）官方文档 + Kimi Code 源码：reasoning_content 回传、固定采样参数、缓存、工具调用坑、重复调用提醒 |

## 跨文档共识（对 xana 最关键）

1. **system 与 tools 从会话第一个请求起固定，历史只追加**：中途修改会使 thinking 失效（Claude 新账号直接 400）、缓存失效；动态内容（记忆、todo、日期、提醒）走消息侧。
2. **进度更新与续跑**：进度说明归一为统一事件并在渠道渲染；todo 的"受阻/取消"须算终态，续跑有上限。
3. **预算信号**：时间预算可常驻；token 剩余倒计时只在阈值处提示（Claude 官方警告常驻倒计时会让模型提前收工）。
4. **不可信内容隔离**：粘贴内容加标签、不可信输入不进 system、按可信来源决定是否据此行动。
5. **Kimi 专属**：完整回传 reasoning_content；不发采样参数；会话内 effort 固定；强制 tool_choice 会 400。

中文版提示词均未经官方验证，重要提示词上线前需在自有任务（含 Kimi）上实测。
