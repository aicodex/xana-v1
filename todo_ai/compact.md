# xana — Auto-Compact + Manual Compact

## 现状

xana **没有 compact 功能。**
- `ModelConfig` 只有 `provider`, `model`, `effort`, `max_tokens`——没有 `context_window` 或 `force_compact`
- `ModelEntry` 有 `compact_override` 字段但没被任何代码消费
- `DefaultHarness._invoke()` 没有 token 计数和阈值检查
- `memory/active_context.py` 有 `compact()` 方法声明，但没有 LLM 摘要实现
- 没有 `/compact` 命令

## 设计约束（必须遵守）

**复用现有接口，不要新建机制。**

| 需求 | 怎么接入 | 已有接口/模块 |
|------|---------|-------------|
| 模型阈值 | `config/contracts.py` 的 `ModelConfig` 扩展 `context_window` + `force_compact` | `ModelConfig` frozen dataclass |
| Token 计数 | 使用已有的 `UsageAccumulator`（Plan 2 已实现） | `harness_sdk/interfaces.py` 的 `UsageAccumulator` |
| 触发逻辑 | `DefaultHarness._invoke()` 里内联 check，不开新文件 | `harness_sdk/default.py` |
| LLM 摘要 | 使用已有的 `ModelClient.stream()` 调同模型，`thinking=disabled`, `max_turns=1`, tool 禁止 | `llm_driver/interfaces.py` 的 `ModelClient` |
| /compact 命令 | 实现已有的 `CommandRouter` 接口，注册到 `DefaultCommandRouter` | `agent_adapter/interfaces.py` 的 `CommandRouter` |
| Prompt 常量 | 放 `harness_sdk/compact_prompt.py`，纯字符串常量，不定义新 class | — |
| 中文 prompt | 抄 CC `services/compact/prompt.ts`，逐段翻译。源文件：`/home/xzp/project/best_agent/claude-code-2.1.88-src/services/compact/prompt.ts` | — |

## Prompt 中文翻译

**抄 CC 的 prompt，用中文。除语言外，格式、段落、结构完全一致。** CC 源码位置：`/home/xzp/project/best_agent/claude-code-2.1.88-src/services/compact/prompt.ts`

**开头行：**
```
此会话正在从上一次因上下文不足而中断的对话中继续。以下摘要涵盖了对话的早期部分。
```

**NO_TOOLS_PREAMBLE（对应 CC prompt.ts:19-26）：**
```
重要：仅回复纯文本。禁止调用任何工具。

- 禁止使用 Read、Bash、Grep、Glob、Edit、Write 或任何其他工具。
- 上述对话中已包含你所需的全部上下文。
- 工具调用将被拒绝且浪费你唯一的回合——你将无法完成任务。
- 你的全部回复必须为纯文本：一个 <analysis> 块后跟一个 <summary> 块。
```

**BASE_COMPACT_PROMPT（对应 CC prompt.ts:61-143，9 段结构完全一致）：**
```
你的任务是创建一份迄今为止对话的详细摘要，密切关注用户的明确请求和你之前的操作。
此摘要应详尽捕捉技术细节、代码模式以及架构决策，这些对于在不丢失上下文的情况下继续开发工作至关重要。

在提供最终摘要之前，将你的分析包裹在 <analysis> 标签中，以组织你的思路并确保覆盖所有必要要点。在你的分析过程中：

1. 按时间顺序分析每条消息和对话的每个部分。对每个部分，彻底识别：
   - 用户的明确请求和意图
   - 你处理用户请求的方法
   - 关键决策、技术概念和代码模式
   - 具体细节，如：文件名、完整代码片段、函数签名、文件编辑
   - 你遇到的错误以及如何修复的
   - 特别注意你收到的用户反馈，尤其是用户告诉你要以不同方式做某事的反馈。
2. 仔细检查技术准确性和完整性，彻底处理每个必需元素。

你的摘要应包含以下部分：

1. 主要请求与意图：详细捕获用户的所有明确请求和意图
2. 关键技术概念：列出讨论过的所有重要技术概念、技术和框架。
3. 文件与代码部分：列举被检查、修改或创建的特定文件与代码部分。特别注意最近的消息，并在适用时包含完整代码片段，并包含此文件读取或编辑之所以重要的原因摘要。
4. 错误与修复：列出你遇到的所有错误，以及你是如何修复它们的。特别注意你收到的特定用户反馈，尤其是用户告诉你要以不同方式做某事的反馈。
5. 问题解决：记录已解决的问题以及任何正在进行的故障排除工作。
6. 所有用户消息：列出所有非工具结果的用户消息。这些对于理解用户的反馈和变化的意图至关重要。
7. 待处理任务：概述你被明确要求处理的任何待处理任务。
8. 当前工作：详细描述正好在此摘要请求之前正在进行的工作，特别注意来自用户和助手双方最近的消息。在适用时包含文件名和代码片段。
9. 可选的下一步：列出你要采取的与最近工作相关的下一步。重要提示：确保此步骤与用户最近的明确请求以及你在此摘要请求之前正在处理的任务直接一致。如果你的上一个任务已经结束，那么只有在其与用户请求明确一致的情况下才列出下一步。在没有先与用户确认的情况下，不要开始无关的请求或早已完成的旧请求。如果存在下一步，包含来自最近对话的直接引用，准确显示你正在处理什么任务以及你在哪里停了。这应该是逐字的，以确保任务解释没有漂移。

以下是一个输出应如何结构的示例：

<example>
<analysis>
[你的思考过程，确保所有要点被彻底且准确地覆盖]
</analysis>

<summary>
1. 主要请求与意图：
   [详细描述]

2. 关键技术概念：
   - [概念 1]
   - [概念 2]
   - [...]

3. 文件与代码部分：
   - [文件名 1]
      - [此文件之所以重要的原因摘要]
      - [对此文件所做的更改摘要（如有）]
      - [重要代码片段]
   - [...]

4. 错误与修复：
    - [错误 1 的详细描述]：
      - [你是如何修复该错误的]
      - [关于该错误的用户反馈（如有）]
    - [...]

5. 问题解决：
   [已解决问题和正在进行的故障排除的描述]

6. 所有用户消息： 
    - [详细的非工具使用的用户消息]
    - [...]

7. 待处理任务：
   - [任务 1]
   - [...]

8. 当前工作：
   [当前工作的精确描述]

9. 可选的下一步：
   [可选的下一步]

</summary>
</example>

请根据迄今为止的对话提供你的摘要，遵循此结构并确保你的回复精确且详尽。
```

**NO_TOOLS_TRAILER（对应 CC prompt.ts:269-272）：**
```
提醒：禁止调用任何工具。仅回复纯文本——一个 <analysis> 块后跟一个 <summary> 块。工具调用将被拒绝，你将无法完成任务。
```

**post-compact 消息格式（抄 CC `getCompactUserSummaryMessage`，prompt.ts:337-374）：**
```
此会话正在从上一次因上下文不足而中断的对话中继续。以下摘要涵盖了对话的早期部分。

{formattedSummary}

如果你需要压缩前的具体细节（如确切的代码片段、错误信息或你生成的内容），请阅读完整转录记录：{transcriptPath}

最近的消息被逐字保留。
继续对话，无需向用户提出任何进一步的问题。直接继续——不要确认摘要，不要复述正在发生的事情，不要以"我将继续"或类似语句开头。如同中断从未发生一样继续上一个任务。
```

**boundary marker：** `对话已压缩`

## 算法

1. 从上次 compact boundary 后收集消息
2. 去掉 image/document 块
3. `ModelClient.stream()` 调同模型（`thinking: disabled`, `max_turns: 1`, 无 tools）
4. 提取 `<summary>` 文本 → `formatCompactSummary()`
5. 保留最近 user 消息（20K token budget，Codex 风格）
6. 注入 `[boundary_marker, summary_message, ...recent_user_messages]`
7. PTL 重试最多 3 次 + circuit breaker

## 阈值

- `force_compact`: 触发自动 compact 的 token 线（CC 风格: `context_window - max_output - 13K`）
- `blocking`: `context_window - 3K`——阻止 API 调用，提示用户 `/compact`
- `warn`: `force_compact - 20K`

## 验证

1. `ModelConfig` 包含 `context_window` + `force_compact`
2. token 超 `force_compact` → `DefaultHarness._invoke()` 触发 compact
3. LLM 摘要成功，post-compact messages 包含 boundary marker + summary
4. `/compact` 命令手动触发成功
5. 连续 compact 不会死循环（boundary marker 防重复 + circuit breaker）
