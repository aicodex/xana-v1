---
title: todo_ai
type: todo
status: active
---

# todo_ai

> TODO is a special memory tree for executable work items.
> `doc_ai/` maintains current detailed design. `exp_ai/` maintains experience and rationale. `todo_ai/` maintains pending work and execution scratch.

## 权威规则

- TODO is not target architecture truth; if a TODO changes design, update `doc_ai/` first.
- TODO files are allowed to be deleted after completion.
- Implementation plans are generated at execution time, not preserved forever.
- Every TODO must point to the current design document it builds on.
- Reviewer/user deltas copied here must be classified into scope, constraints, tests, and exclusions.

## 当前队列

- [first_core_flow_v1_leftovers](first_core_flow_v1_leftovers.md) — 第一核心流 v1 已知遗留（token_estimate、provider fallback、CLI 重复输出等），并行接手前先扫
- [doc_split_followups](doc_split_followups.md) — continue splitting design docs as implementation becomes concrete
- [deferred_features](deferred_features.md) — tools/skills 待办,依赖未完成的前置功能(request_user_input、AgentSpawner、sandbox 等)

## 文件命名

- `module_task.md` for module-scoped work.
- `_template.md` for new work item shape.
