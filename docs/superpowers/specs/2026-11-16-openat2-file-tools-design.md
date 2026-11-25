---
title: 基础文件工具 openat2 双模式设计
status: proposed
date: 2026-11-16
---

# 基础文件工具 openat2 双模式设计

## 意图

当前 `Read` 采用 resolve/exists/is_dir 后再 `os.open`，`Write/Edit` 最终直接 `write_text`，存在检查与使用之间的竞态，也没有使用 openat2。此次在共享文件打开边界原位引入 openat2：默认保持现有路径自由度，未来沙箱可显式启用 `RESOLVE_BENEATH`，不建立第二套文件工具。

内核语义来源：[Linux openat2(2) manual](https://man7.org/linux/man-pages/man2/openat2.2.html)。

## 目标与非目标

### 目标

- 共享一个 openat2 实现，供 Read/Write/Edit 及 Grep/Glob 的实际文件检查复用。
- 两种显式模式：
  - `unconstrained`（默认）：`resolve=0`，保留当前绝对路径、`..` 和符号链接行为。
  - `beneath`：`RESOLVE_BENEATH`，以指定目录为根，拒绝绝对路径、越界 `..`、绝对符号链接及逃逸解析。
- 打开后立即用同一 fd 做 fstat/read/write；Edit 在同一 fd 上读取、修改、seek/truncate/write。
- 严格模式的父目录逐级通过 dirfd 创建/打开，不以 `mkdir(parents=True)` 重新引入路径竞态。
- 默认模式在 Linux 外保持现有行为；严格模式在非 Linux 或不支持 openat2 的内核上明确 `unsupported`，不静默降级。
- 保持现有工具名称、参数、输出结构和默认权限行为。

### 非目标

- 不默认开启沙箱，不把 permission 的 manual/yolo/auto 改成路径模式。
- 不实现完整的文件系统沙箱、挂载隔离、网络隔离或 `RESOLVE_IN_ROOT` 第三模式。
- 不禁止根目录内部的相对符号链接；严格模式针对逃逸，不是禁绝所有链接。
- 不在本次重写 Python glob 枚举器；枚举结果的普通文件检查/内容读取必须经过 openat2。

## 方案比较与选择

### A. 共享 openat2 + unconstrained/BENEATH（已选择）

一个边界同时保留默认兼容性与未来沙箱能力。绝对路径在 BENEATH 下按内核语义拒绝，调用方严格模式应使用相对路径。

### B. 默认 RESOLVE_IN_ROOT

会把所有绝对路径解释到 root 内，改变现有默认行为；用户已明确不采用。

### C. RESOLVE_NO_SYMLINKS 或仅 O_NOFOLLOW

会误伤合法内部链接，且不能覆盖 `..` 与完整 TOCTOU 问题，排除。

## 配置与模式归属

在 `policy/contracts.py` 增加：

```python
PathResolutionMode = Literal["unconstrained", "beneath"]

class SandboxPolicySpec:
    ...
    path_resolution: PathResolutionMode = "unconstrained"
```

`ToolSpec.sandbox_policy` 已存在但目前无消费者，本次直接消费它，不新增平行配置：

- `None` 或 `path_resolution="unconstrained"`：resolve=0，根目录仅用于相对路径的 dirfd。
- `path_resolution="beneath"`：resolve=RESOLVE_BENEATH，根目录取 `sandbox_policy.cwd`；没有该配置时严格模式无隐式根，不启用。

现有内置工具默认不变。未来沙箱通过现有 ToolSpec 注入 `SandboxPolicySpec(cwd=..., path_resolution="beneath")` 即可启用，不修改 ToolContext.metadata。

## 共享实现

新增 `tools/builtin/_openat2.py`：

- 用 `ctypes.CDLL(None, use_errno=True).syscall(SYS_openat2=437, dirfd, path, open_how, 24)`；`open_how` 为 flags/mode/resolve 三个 zero-filled `c_ulonglong`。
- `open_file(path, cwd, flags, mode=0o666, resolution="unconstrained") -> int`：返回 fd，调用方负责关闭。
- `open_write(path, cwd, resolution) -> (fd, created)`：先 O_CREAT|O_EXCL，存在则重新以 O_TRUNC 打开，action 不靠 exists 竞态判断。
- `mkdir_and_open_write` 仅严格模式需要逐级创建父目录；默认模式可先沿用现有 parent.mkdir，再 openat2。
- `stat_regular(path, cwd, resolution) -> stat_result | None`：openat2 后 fstat，供 Glob/Grep 排除非普通文件并读取 size/mtime。
- 严格模式遇到 EAGAIN 可重试至多 3 次；EXDEV/ELOOP 映射 `sandbox_denied`。ENOENT 保留 not_found 语义。
- ENOSYS/非 Linux：默认回退 `os.open`；严格模式抛带 `failure_kind="unsupported"` 的明确错误。其它异常不伪装成传输或文件不存在。
- 路径只用于内核解析和展示，不用 `Path.resolve()` 在打开前跟随链接。

## 工具改造

### Read

删除 exists/is_dir 预检，直接 openat2；同一 fd 完成 fstat、普通文件检查、大小限制与读取。ENOENT 返回 not_found，目录/二进制等维持 validation。

### Write

父目录按模式处理后，通过 open_write 原子判断 created/overwritten 并写回。保留 protected_dir 的现有权限门禁及 post_tool_use reindex。

### Edit

同一 fd 以 O_RDWR 打开，fstat 后读取原文；完成 old_string 校验后 seek(0)、写入、truncate。没有匹配或多处匹配时不写。保留 diff 与 reindex。

### Glob / Grep

- Glob 的候选普通文件判断和 mtime 读取改用 openat2+fstat；逃逸/无法打开的候选不进入结果。
- Grep worker 不再先 `path.stat()` 再 `os.open()`；从参数接收 path_resolution，直接 openat2 后在同一 fd 检查普通文件、大小和内容。
- 默认输出不变；严格模式根目录本身逃逸时返回 sandbox_denied。

## 验证

### 单元与契约

- 默认模式：workspace 外绝对路径仍可打开，内部/外部符号链接与 `..` 行为保持。
- BENEATH：拒绝绝对路径（即使绝对路径指向根内）、越界 `..`、绝对符号链接、相对链接逃逸；允许根内相对符号链接。
- Write created/overwritten、深层父目录、Edit 同 fd 写回、Glob/Grep 文件检查。
- ENOSYS 默认回退、严格 unsupported；EXDEV→sandbox_denied；EAGAIN 有界重试。
- 竞争压力：反复切换父目录/文件链接时，严格模式只能读到根内内容或明确失败，绝不能返回根外秘密。
- 现有 `test_tool_read/write/edit/glob/grep` 默认套件全部通过，canonical fake pipeline 不变。

### 真实环境与全量

- 在当前 Linux 5.15 主机做真实 openat2 打开/写入/BENEATH 拒绝验证。
- `uv run pytest` 全量无新增失败；`tools/check_arch_contracts.py` 不隐藏新增违例。
- 更新 `doc_ai/tools.md`、`tools_spec.md`、`tools_lifecycle.md`、`policy.md`：两模式、绝对路径严格语义、非 Linux 降级边界和测试证据。

## 交付边界

默认行为不应因这次升级而收紧；只有显式 beneath 的沙箱策略才拒绝 workspace 外路径。文档和验收不得把 unconstrained 描述为安全沙箱，也不得把 beneath 描述为完整隔离系统。
