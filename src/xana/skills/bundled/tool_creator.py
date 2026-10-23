"""tool_creator skill — 把一个命令行程序派生成有类型参数的工具(docs/design/iteration-agent-design.md §5.3)。

流程:弄清能力 → 起草 TOOL.md → tool_define test 试跑(含过去时点)→ 用户确认 → tool_define create。
"""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.skills.contracts import SkillActivation, SkillContribution, SkillManifest, SkillPackage, SkillSpec
from xana.skills.interfaces import Skill

_BODY = """# tool_creator — 把命令行程序派生成工具

用法:某个 agent 需要执行能力(跑一个数据查询脚本、处理文件的程序……)时使用。除 chat 与 iterator 外,
agent 默认不能用 Bash;需要执行能力就为具体程序派生一个工具,只开放声明过的参数。

第一步:弄清能力
1. 要包装哪个程序?命令前缀是什么(如 ["./qt", "--format", "json"])?在哪个目录运行?
2. 有哪些子命令?每个子命令哪些参数允许模型填?类型、是否必填、默认值、取值范围?
3. 结果和时间的关系(temporal,必填):
   - static:结果不随时间变化(处理给定文件、计算)。
   - as_of:能按研究时点截断(只返回某日期前已公开的数据)。写明 as_of.arg(如 --as-of)或 as_of.env;
     这个参数由评测环境注入,不要放进模型可填的参数里。
   - live:只能取当前状态(实时行情、当前文件系统状态)。
4. 是否改变外部状态(destructive,必填):写文件、移动文件、发消息、下单都算 true。

第二步:起草 TOOL.md
```
---
kind: tool
name: qt
description: A 股行情与财务数据(本地,只读)
command: ["./qt", "--format", "json"]
cwd: project
subcommands:
  income:
    description: 利润表与财务指标
    args:
      code: {type: string, description: 代码,如 300476.XSHE}
      quarters: {type: integer, flag: --quarters, default: 8}
temporal: as_of
as_of: {arg: --as-of}
destructive: false
timeout_s: 60
---
给模型看的使用说明:各子命令什么时候用、输出字段含义、常见错误。
```
参数没有 flag = 位置参数(按声明顺序);boolean 参数必须有 flag。
个别子命令与工具级声明不同时,在该子命令下单独写 temporal / as_of 覆盖,例如:
```
  daily:
    description: 日线行情
    temporal: live            # 没有 --as-of:评测环境里只回放或拒绝,不会被注入时点参数
    args: {code: {type: string}}
```
声明要与程序实际行为一致:接受 --as-of 却不真的截断的子命令应声明为 live(否则评测时会泄漏未来信息)。

第三步:试跑
调用 tool_define(action="test", content=草稿, input=示例参数),确认输出正确。
temporal=as_of 的工具再带 as_of=过去某天试一次,确认结果确实截断到那天之前。

第四步:确认
把 TOOL.md 全文和试跑结果给用户看,用户明确同意后调用 tool_define(action="create", content=...)。

第五步:接到 agent 上
告诉用户哪些 agent 可以在 AGENT.md 的 tools 里加上这个工具名;替代 Bash 时同时去掉 exec。
"""


class ToolCreatorSkill(Skill):
    def __init__(self) -> None:
        self._spec = SkillSpec(
            component=ComponentSpec(
                kind="skill", name="tool_creator", qualified_name="bundled.tool_creator",
                description="把命令行程序派生成有类型参数的工具(起草 TOOL.md → 试跑 → 确认 → tool_define 注册)。",
                source=ComponentSource(source_type="builtin", scope="system"),
            ),
            when_to_use="agent 需要执行某个程序的能力、但不应拿到 Bash 时;或用户要把一个脚本包装成工具时。",
            package=SkillPackage(root=None, manifest=SkillManifest(name="tool_creator", description="派生工具"),
                                 instructions_path=None),
        )

    def spec(self) -> SkillSpec:
        return self._spec

    async def activate(self, activation: SkillActivation) -> SkillContribution:
        return SkillContribution(prompt_blocks=(_BODY,))

    async def deactivate(self, session_id: str) -> None:
        return None
