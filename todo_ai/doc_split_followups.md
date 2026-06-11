---
title: doc split followups
type: todo
status: active
---

# doc_split_followups — continue detailed design split when modules become concrete

**对应设计**: [doc_ai/README_ai.md](../doc_ai/README_ai.md)
**默认执行**: manual_design
**状态**: pending

## 需要做什么

- As implementations become concrete, replace placeholder topic docs with real class-level design.
- For each concrete class, add a dedicated topic doc when the behavior is non-trivial.
- Keep `module.md` as module boundary, not implementation dump.

## 约束 / 不变量

- `doc_ai` is current design truth.
- `todo_ai` is pending work, not design truth.
- `exp_ai` stores rationale and mistakes.
- Links must stay relative so the memory package can be copied to `.xana`.

## 测试要点

- [ ] `MarkdownMemoryTree` builds the copied memory package.
- [ ] lint reports zero dangling links.
- [ ] search can find the target module and detailed docs.
