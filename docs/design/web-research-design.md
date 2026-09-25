---
title: 联网调研(web_research / deep_research)设计
status: 第一阶段已实现(独立服务 + 客户端工具);harness 并行执行待做
date: 2026-09-26
---

# 联网调研设计

## 0. 目标与形态

让任意主模型都能完成"并行搜索几十个网页，然后给出结论"的调研，交互上模仿 ChatGPT/Codex 的服务端搜索：
调用方简单（只决定搜什么、搜多深），工具本身强（并行搜索、抓取、阅读、核对、引用都在服务里完成）。

**独立服务 + 客户端工具**：

| 部分 | 位置 | 职责 |
|---|---|---|
| xana-search 服务 | `~/project/xana-search`（独立仓库，见其 README） | 自建 SearXNG、多出口抓取、4B 并行阅读与引用校验、27B 深度搜索任务、Bearer token 鉴权、模型容器启停 |
| 客户端工具 | `src/xana/tools/builtin/xana_search.py` | `web_research`（同步）、`deep_research`（提交 / 长轮询 / 取消）；只做参数校验、带 token 的请求、结果渲染 |
| 配置 | `system.json` → `web_search.xana_search_url` / `xana_search_token` | 为空则不注册这两个工具（`boot.py`） |

模型：规划 Qwen3.8-27B（GPU 0–3，复用已有 `rail_27b`），阅读 Qwen3.5-4B（GPU 4–5，数据并行，服务托管，空闲 6 小时停止）。
方案依据见 xana-search 的 `docs/research.md`（开源爬虫、正文抽取、PDF、反爬、SearXNG、代理轮换、深度搜索系统调研）。

## 1. 工具接口

### `web_research`

```json
{"queries": ["隆基绿能 2025 年报 营收", "光伏组件 出货量 排名 2025"], "objective": "比较组件龙头 2025 年营收与出货量", "read": 30}
{"urls": ["https://..."], "objective": "..."}
```

- `queries` 1–8 条并行执行；`read` 0–60，打开并阅读的网页数（深度由调用方决定）；`urls` 直接读指定网页（≤30）。
- 返回：统计行（"并行搜索 5 个查询，得到 209 条结果；阅读 30 个网页（抓取成功 24，相关 13），用时 31.6 秒"）→ 相关来源的要点（每条附原文引用）→ 其余来源一行一条；全文目录路径供 read/grep 回查。
- 卡片显示 `display_text`：`搜索 5 个查询 · 阅读 30 个网页 · 相关 13 · 31.6 秒`。

### `deep_research`

- 提交：`{"question": "...", "preset": "standard|deep"}` → 立即返回 job_id 和预计时长。
- 查询：`{"job_id": "...", "wait_s": 600}` → 长轮询，任务结束提前返回；进行中返回阶段、查询数、网页数、证据数与最近进展。
- 取消：`{"job_id": "...", "cancel": true}`。

## 2. 实测（2026-09-25/26）

| 场景 | 结果 |
|---|---|
| 快速搜索：5 查询、读 30 页 | 32 秒；抓取成功 24，相关 13 |
| Kimi + web_research：宁德时代 vs 比亚迪 2025 营收与净利润 | 两次调用，27 秒给出带出处的对比表 |
| 深度搜索 standard：光伏四龙头经营与估值对比 | 284 秒；16 查询、48 页、94 条证据；核对轮修正 1 处排序错误；市值/PB 未找到（如实列入"未能确认"） |
| 4B 冷启动 | 快速搜索不等待，返回正文开头并注明；后台拉起容器（约 7 分钟就绪） |
| 空闲停止 / 唤醒 | 真实 docker 测试：docker run → 空闲超时 docker stop → 需要时 docker start |

发现并已处理的问题：小模型把 `RMB27,045mn` 写成"27,045 亿元"（加了单位校验）；相关性判得过严（改为与研究目标任一部分相关即相关，证据从 13 条增到 94 条）；国内引擎单 IP 限流（每出口一份引擎实例、限速、短挂起）；需要 JS 的站点（patchright 渲染兜底，拦截页识别）；27B 开思考写报告太慢（关闭思考，另加核对轮）。

## 3. 待做

### 3.1 harness 并行执行工具调用（三家做法）

现在 `default.py` 逐个执行同一轮里的工具调用。调研结论：

| | Claude Code | Codex | Kimi Code |
|---|---|---|---|
| 并行判定 | 每次调用按输入判定 `isConcurrencySafe`（Bash 只读命令可并行），默认不安全 | 每个工具声明 `supports_parallel_tool_calls`，读写锁：可并行拿读锁，否则写锁独占 | 声明资源访问（文件读/写/搜索、none、all），只有冲突的调用排队；未声明 = all |
| 上限 | 默认 10（`CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`） | 无 | 无 |
| 流式时就开始执行 | 是（每个 tool_use 块结束即启动） | 是（每个 function_call 完成即 spawn） | 否（整条回复结束后成批启动） |
| 写回历史顺序 | 按模型发出的顺序（配对靠 tool_use_id） | 按发出顺序（FuturesOrdered） | 按发出顺序 |
| 一个失败 | 2.1.88 只有 Bash 出错会取消兄弟调用，2.1.281 已移除 | 互不影响 | 互不影响 |
| 用户中断 | 未完成的写合成结果 | 100ms 宽限后中止，写 "aborted by user after Ns" | 2 秒宽限，写合成的中断结果 |
| 审批 | 各自申请，弹窗排队 | 可同时等待多个审批 | 整批审批完才开始执行 |

建议照 Codex 的简单形态：读取已有的 `ToolSpec.concurrency`，同一轮里 `parallel` 的调用并发执行（上限 10，同 CC），其余独占；结果按发出顺序写回。`web_research`/`deep_research` 已声明 `parallel`。执行逻辑放到独立文件，不再加进 `default.py`（已 1806 行）。

### 3.2 其他

- 工具执行中的进度事件（`ToolContext.report_progress` → `tool_progress`），让卡片显示"阅读 18/30 个网页"。**契约变更，需确认。**
- 旧 `web_search`/`web_fetch`（Brave/DuckDuckGo/DeepSeek 原生搜索、`/search_provider`）与新工具并存；是否删除旧路径、迁移 `search_provider` 配置，**需确认**。
- xana serve 需要重启才会注册新工具。
- 结构化行情/财报数据工具（市值、PB 等不适合从网页抓）。
