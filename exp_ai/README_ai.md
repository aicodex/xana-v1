---
title: exp_ai
type: experience
status: active
---

# exp_ai

`exp_ai` records experience, changes, mistakes, and migration notes.

## 设计入口

- [root](../README_ai.md) — 根索引
- [doc_ai](../doc_ai/README_ai.md) — 目标架构权威
- [system](../doc_ai/system.md) — 当前系统边界

## 命名规则

- `module_topic.md`：挂到某个模块的经验。
- 经验可以被模块设计或 detail 文档用相对链接引用。

## 源码调研

- [后台任务等待与完成唤醒](harness_background_task_review.md) — Codex、Claude Code、Kimi Code 的源码机制、Xana 缺口与建议验收场景；2026-11-09。
- [Codex 插话机制](context_codex_steering.md) — 单循环、插话、等待与执行取消分离；2026-11-08。

`exp_ai` records experience, changes, mistakes, and migration notes.

## 设计入口

- [doc_ai](../doc_ai/README_ai.md) — 目标架构权威
- [System Design](../doc_ai/system.md) — 当前系统边界

## Rules

- This directory is not the architecture source of truth.
- Every note should include date, context, decision, and follow-up.
