---
title: memory rules gate
type: doc
status: active
---

# memory rules gate

## 上级模块

- [memory](memory.md) — memory 模块设计
- [memory_markdown_backend](memory_markdown_backend.md) — markdown + SQLite concrete backend
- [tools](tools.md) — 内置工具清单

## 代码锚点

- [memory_write.py](../src/xana/tools/builtin/memory_write.py) — `MemoryWriteTool`
- [boot.py](../src/xana/runtime/boot.py) — `MemoryWriteTool(tree=tree, engine=engine)` 注册（boot.py:220）

## 职责

L2 规则层的**唯一写入口**。规则条目存 `{memory_root}/L2/rules/*.md`，复用 `MarkdownMemoryTree` 节点空间；裸 Write/Edit 进 `L2/` 目录被 policy 拒绝（见 [tools_permission_sandbox](tools_permission_sandbox.md)），本工具是唯一合法变更路径。

## 三动作

| action | 语义 | 前置 |
|---|---|---|
| `create` | 新建条目 | **强制查重**：先跑 top-k 相似度搜索，命中 ≥ 阈值即拒绝，要求改用 refine/supersede |
| `refine` | 重写既有条目正文（frontmatter `status` 保留） | 目标文件必须已存在 |
| `supersede` | 写新条目 + 把旧条目 frontmatter 翻成 `status: deprecated` + `superseded_by: <新路径>` | `supersedes` 必填，目标必须已存在 |

supersede **从不删文件**：旧条目保留在盘上（frontmatter 标记 deprecated），形成可回溯的替代链。

## 查重门禁（create）

- 查询文本 = `title\ncontent[:500]`，token 集 = 正则 `[A-Za-z0-9_\-/\.]+` 小写切词。
- 走共享 `MemorySearchEngine.search(top_k=5)`，对每个命中（排除自身 path）算 `|query ∩ haystack| / |query|`，haystack = 命中节点 `title\nbody` 的 token 集。
- 默认阈值 **0.6**（构造参数 `similarity_threshold`）。best ≥ 阈值 → 拒绝，错误信息带命中路径与相似度，提示 refine/supersede。

## frontmatter 契约

条目文件是标准 markdown + YAML frontmatter，字段：`title` / `status` / `evidence` / `supersedes` / `updated`。

- `create`：`title` + `status: candidate` + `evidence`（支撑本规则的 run 指针，如 `runs/r12.jsonl`）+ `created` 时间戳。
- `refine`：保留原 frontmatter（含 status），覆写 `updated` 时间戳。
- `supersede`：新条目写 `status: candidate` + `supersedes: <旧路径>`；旧条目翻 `status: deprecated` + `superseded_by: <新路径>` + `updated`。
- `status: deprecated` 的条目从搜索结果中过滤（engine 侧 `_is_deprecated`，见 [memory_search_engine](memory_search_engine.md)）。

## 权限与路径安全

- `check_permission` 恒 `allow`——本工具本身就是受制裁的写路径，门禁做在「L2 裸写被 Write/Edit 拒绝」那一侧。
- `_normalize_path`：拒绝绝对路径与 `..`；自动补 `L2/rules/` 前缀与 `.md` 后缀；最终路径恒落在 `{tree.root}/L2/rules/*.md`。
- `validate` 逐 action 校验必填项（create/supersede 要 title+content，refine 要 content，supersede 要 supersedes）。

## 写后索引联动

每次落盘（create/refine/supersede 的新旧两文件）都同步调 `MarkdownMemoryTree.reindex_paths()`，把变更立即 mirror 进 `MemorySearchEngine`——`memory_search` 与 L1 注入下一轮即见新内容（机制见 [memory_markdown_backend](memory_markdown_backend.md) L1 索引联动）。

## 不变量

- L2 规则只能经 memory_write 变更；裸写被 Write/Edit 的 protected_dir 门禁拒绝。
- supersede 不删文件，只翻 frontmatter；deprecated 条目对搜索不可见但保留证据链（`evidence` / `superseded_by`）。
