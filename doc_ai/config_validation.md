---
title: config validation
type: doc
status: active
---

# config validation

## 上级模块

- [config](config.md) — 模块设计

## 代码锚点

- [loader.py](../src/xana/config/loader.py) — `JsonSystemConfigReader`、`write_system_config`
- [system_model.py](../src/xana/config/system_model.py) — pydantic 校验与字段级 validator

## 职责

保证进入运行时的配置是形状合法、语义自洽的。当前只有"读取时校验"；合并后校验与变更监听未实现。

## 已实现：读取时 pydantic 校验

校验发生在 `JsonSystemConfigReader.read()`：JSON 解析后立刻 `SystemConfig(**raw)`，shape 错误（缺必填、类型不符、顶层未知键——`extra="forbid"`）抛 pydantic `ValidationError`，boot 直接失败、不带病运行。字段级 validator 包括：

- `PolicyConfig.default_mode` 必须在 `(default, gated, auto, workspace_gated)` 白名单内；
- `DaemonConfig.bind` 为 `None`/`""` 时自动升级 `127.0.0.1`；
- `ModelEntry` 顶层 `extra="forbid"`，但 channels 段 `extra="allow"`（保留渠道专有扩展键）。

**没有单独的"合并后 validate 阶段"**——因为当前只有 system 一层参与解析（见 [config_merge](config_merge.md)），读取时校验已覆盖全部形状错误。

## 无热重载

system.json 只在 boot 时读一次（I16，见 [runtime_boot](runtime_boot.md)），运行时手写改配置需重启 serve 生效。

## 镜像写的一致性

`write_system_config(path, system)`（loader.py）供 SessionResolver 做镜像写：tmp + fsync + `os.replace` 原子替换，chmod 600，内容相同则跳过（避免热路径 inode 抖动）。写失败只记日志不抛出（I15：内存为准，磁盘为 best-effort 镜像）。

## 不变量

- 校验失败 = 启动失败，不存在"告警但继续"的带病配置路径。
- 运行时永不重读 system.json；变更生效的唯一方式是重启。

## 验收要求

- 输入/输出与 [config](./config.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
