---
title: memory write
type: doc
status: active
---

# memory write(项目记忆的写入)

## 上级模块

- [memory](memory.md) — memory 模块设计
- 设计:[memory-recall-design](../docs/design/memory-recall-design.md) §3.4、§4

## 代码锚点

- [memory_write.py](../src/xana/tools/builtin/memory_write.py) — `MemoryWriteTool`
- [store.py](../src/xana/memory/store.py) — `ProjectMemoryStore`、`migrate_legacy`
- [boot.py](../src/xana/runtime/boot.py) — `MemoryWriteTool(store=memory_store, tree=tree)` 注册;Write / Edit 的 `protected_dir` 含 `memory/`
- [sync.py](../src/xana/cli/commands/sync.py) — `_migrate_memories`

## 存储格式

一条记忆一个文件 `{project_root}/memory/<slug>.md`(slug:小写字母 / 数字开头,可含 `-` `_`,≤64):

```markdown
---
name: 选股偏好
description: 偏好量价齐升、困境反转;推荐时按想象空间排序
type: user          # user | feedback | project | reference
pinned: true        # 可选:每个会话注入全文
updated: 2026-10-19
---
正文
```

手工编辑的文件加载时容错:缺 `description` 取正文第一句,缺 `type` 记为 project。

## 三动作

| action | 语义 |
|---|---|
| `create` | name / description / type / content 必填;同名文件已存在,或与已有记忆的名称 + 描述相似度(两字片段 Jaccard)≥0.6 → 拒绝并提示 update |
| `update` | 只改给出的字段(name / description / type / content / pinned),其余保持 |
| `delete` | 删除文件 |

写入 / 删除后 `tree.reindex_paths` 增量更新 memory_search 索引。`memory/` 对 Write / Edit 受保护(policy 检查点拒绝,提示改用 memory_write),保证 frontmatter 完整。

## 迁移(一次性)

`xana sync` 对所有项目执行 `migrate_legacy`:旧 `memory/*.md`(无 `type`,`title / status` 字段)与 `L2/rules/*.md` → 新格式(`name` = title、描述取正文第一句,`status / evidence / created` 去掉,`deprecated` 的不迁);迁移前复制到 `~/.xana-memory-backup/<时间>/`;迁完删除空的 `L2/`。幂等。

原 L2 规则门禁(create 查重 / refine / supersede 链)只剩 evolution 变更单在用,已移到 `evolution/change_gate.py`(见 [evolution_module](evolution_module.md))。
