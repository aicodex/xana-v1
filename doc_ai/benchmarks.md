---
title: 官方 benchmark 接入
type: doc
status: active
---

# 官方 benchmark 接入

入口：`src/xana/benchmarks/`。Terminal-Bench 使用 Harbor 官方运行器；τ 使用 τ²-bench 的文本会话协议。
这两个入口评测 Xana harness，任务环境、用户模拟器、判分和汇总由上游负责。
不把交互任务转换成静态 `cases.jsonl`，不修改项目现有的 `eval_run` / `eval_score` 语义。
上游结果目前保存在各自运行器的结果目录，尚未导入 Xana 的候选版本判决或 Web 评测页面。

## 契约与所有权

- `harbor.py` 实现官方 `BaseAgent`；向任务容器安装指定的 Xana wheel，使用容器工作目录和现有文件、Bash、TodoWrite 工具。支持 Linux、与宿主 uv 二进制相同的架构；运行环境需要能下载 Python/依赖。没有导入日常项目的记忆、技能或子 Agent。
- `tau2.py` 实现官方 `HalfDuplexAgent`。官方提供领域 policy 和工具 schema，按原文转发；没有创作评测提示词或重写工具说明。`AGENT_INSTRUCTION` / `SYSTEM_PROMPT` 从官方模块直接加载，属于外部评测协议输入，不维护一份 Xana 翻译副本以免改变评测条件。此入口支持普通文本交互，不支持 voice、solo 或 intake 专用终止协议。
- `process.py` 每个 trial 启动独立进程；`worker.py` 只调用 `boot()` 和 `Agent.send()`，没有第二套模型循环。用户续问复用同一会话；工具调用等待官方结果，通过相同调用 ID 返回现有执行链。工具执行按顺序交付给 τ 运行器；模型中途说明不作为单独的用户回复。
- 上下文仍由 ContextSDK/HistoryMirror 维护。任务给定的初始会话通过 `append_history()` 导入一次；续问不重建 system 或历史。工具错误保留错误标记；缺失工具结果、进程错误和超时明确失败。
- `workspace.py` 仅取显式指定模型，生成 trial 私有配置；不复制渠道、定时任务、日常会话、项目记忆、评测真值。服务无需重启。官方运行器仍可读判分数据，但 adapter 不把 `task` 中的答案或评价准则发给 Xana。
- τ 的 `stop()` / 失败 / 超时关闭进程、删除 trial 凭据、保留会话日志；TB 超时由 Harbor 销毁容器。**不要把这些临时实例作为线上 serve 运行。**

## 安装：两套独立环境

核对版本（2026-11-13）：

| 上游 | 源码版本 | Python |
|---|---|---|
| [Harbor](https://github.com/harbor-framework/harbor) | `06d0e86a8cfbac213548e0caec533afc5d81ff31`（0.25.0） | ≥3.12 |
| [τ²-bench](https://github.com/sierra-research/tau2-bench) | `c06b07dd7c4d4ec61ba6ff9bcdf0b15dd7ef36d4`（2.0.0） | ≥3.12、<3.14 |

这两个版本要求的 LiteLLM 范围不兼容，分开安装。它们不是 Xana 线上运行时依赖。

```bash
# 在 Xana 仓库执行；uv 已安装
uv venv --python 3.12 .venv-tb
uv pip install --python .venv-tb/bin/python -e . \
  'harbor @ git+https://github.com/harbor-framework/harbor.git@06d0e86a8cfbac213548e0caec533afc5d81ff31'

uv venv --python 3.12 .venv-tau
uv pip install --python .venv-tau/bin/python -e . \
  'tau2 @ git+https://github.com/sierra-research/tau2-bench.git@c06b07dd7c4d4ec61ba6ff9bcdf0b15dd7ef36d4'
```

τ 数据需要官方仓库 `data/`，按官方要求设置 `TAU2_DATA_DIR` 到该目录。
本机源码位于 `/home/xzp/project/best_agent/benchmarks/harbor` 和同级 `tau2-bench`。

## 模型与凭据

创建**评测专用** Xana home，例如 `$PWD/.benchmark-home`，在其中的 `config/system.json` 配置真实模型、上下文和输出上限。模型名必须和 `models[].name` 一致；没有默认 fake 或自动降级。

Kimi 登录可使用：

```bash
XANA_HOME="$PWD/.benchmark-home" .venv-tau/bin/xana login
```

本地 τ 子进程通过目录链接共享这个专用 home 的凭据权威文件及现有跨进程刷新锁。
TB 在宿主刷新后只上传 access token，**不上传 refresh token**。运行期间每 30 秒检查宿主权威文件，在到期前 180 秒刷新，并原子替换容器内的 access-token 快照；快照交付失败会中止 trial，取消时也会停止续供。刷新仍使用现有跨进程凭据锁，不创建第二份刷新权威。默认 adapter timeout=1800 秒，可通过 `--ak timeout=900` 调整。

不要将线上服务 home 用于评测。τ 的用户模拟器使用上游 LiteLLM 配置和相应凭据，**独立于 Xana 被测模型**；不会偷偷使用 K3 或其他模型替代未配置项。模型计费和官方整套评测可能较大，先用一个任务、一个 trial、并发 1 验证。

## Terminal-Bench

```bash
uv build --wheel --out-dir dist
.venv-tb/bin/harbor run -d terminal-bench@2.0 \
  -a xana.benchmarks.harbor:XanaAgent -m '<models[].name>' \
  --ak xana_home="$PWD/.benchmark-home" \
  --ak wheel="$PWD/dist/xana-0.1.0-py3-none-any.whl" \
  --ak timeout=900 --ak max_turns=200 \
  -n 1 -k 1 -l 1 --jobs-dir ./benchmark-results/tb
```

数据集版本、任务筛选、次数和最终 pass rate 使用 Harbor 的标准配置。
`agent/xana-result.json` 保存最终回复和 token 用量；`agent/xana-history/` 保存会话；`worker.stderr.log` 保存错误。reward 和异常以官方 trial/job `result.json` 为准。未声明 ATIF/resume/MCP/外部技能支持；不输出虚假的兼容标记。

## τ²-bench

准备官方 `TextRunConfig` JSON，例如 `tau-config.json`：

```json
{
  "domain": "retail",
  "llm_agent": "<Xana models[].name>",
  "llm_args_agent": {"timeout": 600, "max_turns": 200},
  "llm_user": "<已配置凭据的 LiteLLM 用户模拟器模型>",
  "llm_args_user": {"temperature": 0.0},
  "num_tasks": 1,
  "num_trials": 1,
  "max_concurrency": 1,
  "max_steps": 100,
  "seed": 42,
  "save_to": "xana-retail-smoke"
}
```

```bash
TAU2_DATA_DIR=/absolute/path/to/tau2-bench/data \
  .venv-tau/bin/python -m xana.benchmarks.tau2 \
  --config tau-config.json --xana-home "$PWD/.benchmark-home" \
  --artifacts ./benchmark-results/tau-xana
```

入口只注入 `agent=xana` 和两个本地目录，其他任务、试验次数、用户模型配置交给官方 `TextRunConfig` 校验。
`llm_args_agent` 是 Xana adapter 参数（timeout/max_turns），不接受 LiteLLM temperature 等参数；被测模型配置仍以 Xana 模型配置为准。
官方 reward、pass^k、用户模拟器成本和结果文件由上游管理。Xana 会话日志保存在 artifacts，每个 trial 独立；最终回复携带该用户回合的 token 汇总，未产生最终回复的工具阶段以 Xana 运行记录为准，不伪造美元成本。

## 验证

新增测试在仓库已有的独立 `tests/` Git 仓库中；与 Xana 源码需要同步保存。

- `tests/test_benchmark_runtime.py`：真实 provider HTTP 协议 + 子进程 + boot + harness，测试工具往返、错误、续问、初始工具历史、任务隔离、超时与凭据清理。模型响应由本地 HTTP fixture 提供。
- `tests/test_benchmark_tau2.py`：官方 Agent 接口；官方 mock 环境、runner、数据库 evaluator 的端到端回归。
- `tests/test_benchmark_harbor.py`：官方 BaseAgent 契约；显式开启 Docker 后，安装构建的 wheel、执行 Bash、由官方 verifier 检查文件。

```bash
uv pip install --python .venv-tau/bin/python pytest pytest-asyncio
.venv-tau/bin/python -m pytest -q tests/test_benchmark_runtime.py tests/test_benchmark_tau2.py

uv pip install --python .venv-tb/bin/python pytest pytest-asyncio
XANA_TEST_DOCKER=1 .venv-tb/bin/python -m pytest -q tests/test_benchmark_harbor.py
```

Docker 测试使用本地模拟 HTTP 服务、真实容器和官方 verifier，测试镜像为 `python:3.12.12-bookworm`。这些是接入验证，**不是 Terminal-Bench 或 τ² 的真实模型成绩**。另有 Kimi 宿主刷新、access-only 快照交付失败与取消回归。尚未执行付费模型的全量基准。

2026-11-13 验证：运行时/τ 接入与现有架构、eval suite、extension 回归共 **33 passed**；Harbor 契约、wheel 版本校验及 Docker 端到端共 **3 passed**。τ mock 数据库评分和 Harbor 冒烟 verifier 均返回 reward=1。未重启服务；既有 `17777/healthz` 返回正常。

## K2.8 真实模型小样本（2026-11-13）

服务端 `/models` 确认 `kimi-for-coding` 的显示名称为 **K2.8 Preview**。从官方数据中预先固定抽样，seed=42，每题一次模型尝试，不按结果换题。

| 评测 | 有评分样本 | 通过 | 样本通过率 | 未取得评分 |
|---|---:|---:|---:|---|
| τ² retail | 3 | 2 | 66.7% | 0 |
| Terminal-Bench 2.0 | 2 | 1 | 50% | 1 个镜像下载失败，模型未启动 |

| 样本 | 官方 reward | 观察 |
|---|---:|---|
| τ retail `7` | 1 | 正常结束，通过 |
| τ retail `100` | 1 | 正常结束，通过 |
| τ retail `110` | 0 | 用户模拟器在确认操作时同时输出 `###STOP###`，运行器结束会话，Xana 未获得执行三项写操作的下一轮；数据库和动作检查均失败 |
| TB `constraints-scheduling` | 1 | 日历结构、约束、最早时间选择三项验证全部通过 |
| TB `build-cython-ext` | 0 | Agent 超过 900 秒；11 项验证通过 2 项、失败 9 项，扩展构建未完成。运行中多次遇到源码下载问题 |
| TB `train-fasttext` | — | Docker Hub 拉镜像返回 EOF，单独重试拉镜像仍失败；没有启动模型，不计入上述 50% 分母 |

这是接入后的诊断样本，不是全量成绩，不能和排行榜直接比较：

- TB 从官方 89 题中按“无 GPU/TPU、CPU≤2、内存≤4096 MiB”筛出 87 题，再用 seed=42 抽取三题。数据固定在 `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c`。本轮每题 900 秒、max_turns=100；其中 scheduling/fasttext 的时限低于原任务默认值。
- τ 的被测 Agent 与用户模拟器均为 K2.8，用户模拟器 reasoning_effort=low、max_completion_tokens=8192；max_steps=60、并发 1。自然语言断言 evaluator 配置沿用上游默认 `gpt-5.4-mini-2026-03-17`，没有将它标称为 K2.8。该配置不同于上游默认用户模型；第 110 题特别受模拟用户提前终止影响，原始 0 分照录，不据此单独判定 Xana 的执行能力。
- τ 被测 Agent 的 36 次模型调用合计输入 254,702 token（其中缓存读取 227,584），输出 7,972；用户模拟器输入 16,720、输出 3,754。TB scheduling 输入 14,629（缓存读取 9,472）、输出 1,558，共 3 次模型调用。Cython 超时 trial 的汇总用量未完整导出，不能把以上数值称为本轮全部消耗。LiteLLM 缺少 K2.8 定价映射，输出的 0 美元不是已验证的实际成本。
- 首次 TB 启动因短期 token 不足以覆盖 timeout 而失败，发生在模型执行前。补上宿主续供后重跑相同样本，原始错误保留；真实长任务中已观察到宿主和容器快照成功换新，容器 refresh_token 始终为空。
- 本轮暴露出强制超时时日志未完整导出，随后改为从第一条消息起写入 Harbor 日志挂载。该改动不改变当前已运行样本的模型输入/输出；初次 Cython 超时记录无法追补。最新 Harbor 回归 **6 passed**，包含成功/失败的凭据交付、取消、普通 Docker 执行及强制超时后会话日志保留。

本机证据（实验目录被 gitignore 排除，部署前应按需归档）：

- `benchmark-results/20261113-k28-sample/sample-report.json`：汇总，含每题原始结果文件位置。
- 同目录 `tau-results.json`、`tau-config.json`、`tb-manifest.json`：原始 τ 结果与预选样本配置。
- 同目录 `tb/k28-official-sample-renewed/`：Harbor 官方 trial/job 结果与 verifier 输出；初次启动错误在 `tb/k28-official-sample/`。
- 同目录 `train-fasttext-pull-retry.log`、`harbor-regression.log`：镜像重试与回归证据。

本轮没有重启 Xana 服务，没有改动线上项目记忆或定时任务。
