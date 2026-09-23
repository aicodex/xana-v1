---
title: runtime
type: doc
status: active
---

# runtime

## 定位

进程生命周期、`boot()` 组装、daemon HTTP+SSE、定时任务调度外壳。

## 详细设计

- [runtime_boot](runtime_boot.md) — `boot()` 唯一组合点：memory 底座 → config → registry → gateway，`BootResult` 全量出口
- [runtime_lifecycle](runtime_lifecycle.md) — `xana serve` 启动/运行/关闭序列，daemon HTTP（/healthz、token 鉴权、SSE）
- [runtime_scheduler](runtime_scheduler.md) — `SchedulerService`/`FileSchedulerService` + `SchedulerRunner` + cron 工具 + schedule skill 的定时任务闭环

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

Process lifecycle, boot order, shutdown, scheduler shell, and top-level dependency wiring.

## Does Not Own

Agent behavior, tool execution logic, LLM provider logic, memory semantics, or adapter protocol details.

## Inputs

- `project_root`
- `DefaultConfigProvider`（可注入）
- `extension_registry`（可空）
- `history_root`（持久化开关，可空）

## Outputs

- `BootResult`（gateway、全部 registry、state、writer、scheduler 等组装产物）
- daemon HTTP app（`build_daemon_app`）
- runtime health（`/healthz`）
- 定时任务触发（到点 prompt 注回 session）
