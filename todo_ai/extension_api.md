# xana Extension API — 在 contracts/interfaces 纪律上加生命周期钩子

## 为什么 xana 需要 Extension API

xana 已经有 yuxu 没有的东西：contracts/interfaces/implementation 三层分离、ComponentSpec 统一身份、boot() 单一组合根、6 个 hook point 定义。但 hooks 没有接入 DefaultHarness，而且 xana 的扩展模型是"实现 ABC → 在 boot() 里注册"——这要求 boot() 知道所有实现的存在。

Extension API 解决的是：**让 Extension 自己声明"我要在什么时候做什么"，boot() 只负责把 Extension 装进 Registry，Harness 循环在正确的时间点调用它们。**

## 和 yuxu Extension API 的关系

yuxu 的 Extension API 设计（`/home/xzp/project/best_agent/ds_review/todo_extension_api.md`）定义了 10 种 Contributor + ExtensionData + ExtensionRegistry + PromptAssembler + Runtime Service 构造注入。xana 的版本可以更简洁——因为 xana 已经有 hooks 定义、已经有 contracts/interfaces、已经有 DI 组合根。

**xana Extension API = yuxu 的设计思路 + xana 的接口纪律**

## 设计

### Phase 1: 把 Extension 模式加到 xana（依赖：4 个 plan 全部完成）

xana 已有 6 个 hook point（`hooks/contracts.py`）但未接入 DefaultHarness。Phase 1 做三件事：

**1. 新增 `extension_api/` 包（纯接口 + 数据契约）**

```
src/xana/extension_api/
  contracts.py       ← ExtensionData, PromptFragment, ContextFragment,
                       TurnInputContext, PromptSlot, SLOT_ORDER
  interfaces.py      ← 8 种 Contributor ABC:
                       ContextContributor, TurnInputContributor,
                       ToolContributor, ToolLifecycleContributor,
                       TurnLifecycleContributor, ThreadLifecycleContributor,
                       ConfigContributor, ApprovalReviewContributor
  registry.py        ← ExtensionRegistry (frozen dataclass) +
                       ExtensionRegistryBuilder
  assembler.py       ← PromptAssembler (collect → sort → slot → budget)
```

**2. 接入 DefaultHarness**

`DefaultHarness._invoke()` 接受可选 `extension_registry: ExtensionRegistry | None = None`。当非 None 时：

```
turn start  → TurnLifecycleContributor.on_turn_start()
prompt      → PromptAssembler.assemble() 收集 ContextContributor fragments
tool call   → ToolLifecycleContributor.on_tool_start() → dispatch → on_tool_finish()
turn end    → TurnLifecycleContributor.on_turn_stop() / on_turn_abort()
```

当 `None` 时行为不变（向后兼容）。

**3. Runtime Service capability ABCs**

```python
# extension_api/capabilities.py
class SchedulerService(ABC): ...
class SandboxManager(ABC): ...
class AgentSpawner(ABC): ...
```

### Phase 2: 把现有 tool/skill/hook 迁移到 Extension 模式（可选，等有需求时做）

- `ext/tools/` — 每个 tool 一个文件，同时实现 `Tool` ABC + `ToolContributor`
- `ext/skills/` — 每个 skill 一个文件，同时实现 `Skill` ABC + `TurnInputContributor`
- `boot()` 升级——拣选 Extension 而非逐个 `await tools.register(ReadTool())`

### 和 yuxu 版本的区别

| | yuxu Extension API | xana Extension API |
|---|---|---|
| Contributor 数量 | 10 种 | 8 种（去掉 TokenUsageContributor、TurnItemContributor——xana 用 hooks 替代） |
| 接口定义 | ABC 在 `harness/core/extension_api/` | contracts + interfaces 分离，在 `extension_api/` |
| 身份系统 | 无（后加 ComponentSpec） | 已有 ComponentSpec，Extension 直接复用 |
| PromptAssembler | 需要新建 | 同，xana 没有这个逻辑 |
| Hook 接入 | 全新接入 DefaultHarness | xana DefaultHarness 已有 hook 定义位置，接入更自然 |
| DI 组合 | 工厂函数 | boot() 已有 DI 模式，加 registry 参数即可 |

## xana 和 yuxu 统一后的最终架构

```
                    yuxu                            xana
                   ────────                       ────────
Harness ABC      harness/core/                  harness_sdk/interfaces.py
Tool/Skill ABC   harness/core/tool.py           tools/interfaces.py
Extension API    harness/core/extension_api/    extension_api/
Runtime impl     harness/runtime/               harness_sdk/default.py
Agent impl       agents_sdk/                    agents/chat_agent.py
Tools impl       ext/tools/                     tools/builtin/
Gateway          agent_gateway/                 agent_adapter/
Memory           memory_store/                  memory/
Composition      create_chat_agent()            boot()
```

两套代码共享同一个设计理念，但 xana 的 contracts/interfaces/implementation 三层分离是 yuxu 后续 Phase 2 要对齐的目标。

## 前置条件

- [x] xana 4 个 plan 全部完成
- [x] `SessionConfigStore` 已创建（Plan 1）
- [x] `DefaultUsageAccumulator` 已创建（Plan 2）
- [x] `TodoWrite` / `EnterPlanMode` / `EnterWorktree` 已创建（Plan 3）
- [x] `FeishuAdapter` / `TelegramAdapter` 已创建（Plan 4）

## 验证

1. `TestExtension` 实现所有 8 种 Contributor，验证钩子在正确时机被调用
2. 现有 e2e 测试全部通过（`extension_registry=None` 行为不变）
3. 写一个 `CodeReviewerAgent`，仅靠组装 Extension + boot() 注册，不写新的 Agent 子类

---

## 防膨胀规约（实现 Extension API 时必须遵守，之后所有开发也必须遵守）

### 规则 0: 永远使用现有接口

**加任何功能前，必须先检查 `src/xana/interfaces.py` 和对应模块的 `contracts.py` + `interfaces.py`。** 如果现有 ABC 能覆盖你的需求，直接实现它。禁止绕开已有接口另起炉灶。

```python
# ✅ 正确：实现已有 ABC
class MyTool(Tool):
    def spec(self) -> ToolSpec: ...
    async def execute(self, ctx, input, progress) -> ToolResult: ...

# ❌ 错误：不实现 Tool ABC，自己从头写
class MyTool:
    def run(self, ctx, input): ...
```

### 规则 1: 接口不够 → 先问用户

如果现有接口确实覆盖不了你的需求，禁止自己加 ABC。必须先向用户报告：

1. 你要做什么功能
2. 为什么现有接口不够（具体到哪个 ABC 的哪个方法）
3. 建议新增什么接口（放在哪个模块的 `interfaces.py`，什么方法签名）
4. 用户同意后，先在 `interfaces.py` 加接口，再写实现

**新接口只能加在 `src/xana/*/interfaces.py` 或 `harness_sdk/interfaces.py`。扩展模块文件只能实现接口，不能定义新接口。**

### 规则 2: 每个模块遵循 contracts/interfaces/implementation 三层

```
新模块/
  contracts.py    ← frozen dataclass，零行为
  interfaces.py   ← ABC + Protocol，零实现
  impl.py         ← 具体实现，import contracts + interfaces，不定义新类型
```

新增模块前，先写 contracts.py → 再写 interfaces.py → 再写实现。禁止一个文件里同时有数据定义 + 接口定义 + 实现代码。

### 规则 3: 层边界不可逆

```
src/xana/
  *_sdk/           ← 只放 ABC + contracts，不 import 任何实现
  runtime/         ← 具体实现，可以 import *_sdk
  agents/          ← 可以 import *_sdk + runtime
  tools/builtin/   ← 实现 Tool ABC，不定义新接口
  skills/builtin/  ← 实现 Skill ABC，不定义新接口
  agent_adapter/   ← 实现 Adapter ABC，不 import agents
```

### 规则 4: boot() 是唯一组合点

所有组件（tool、skill、adapter、agent）的注册和连接**只能**发生在 `runtime/boot.py` 或显式的工厂函数中。禁止在任何其他文件中调用 `register()` 或创建组件实例。

### 规则 5: 构造注入，禁止全局单例

```python
# ✅ 正确
class CronCreateTool(Tool):
    def __init__(self, scheduler: SchedulerService):
        self._scheduler = scheduler  # 构造注入

# ❌ 错误
from xana.runtime import get_scheduler  # 全局单例
```

### 规则 6: 每个文件独立可 import

任何 `ext/` 或 `tools/builtin/` 下的文件必须可以单独 `import`，不依赖同目录其他文件。禁止 `__init__.py` 做集中注册或收集。

### 规则 7: 写入 CLAUDE.md——确保 100% 被读到

**以上规则必须写入 `CLAUDE.md`，放在文件开头。** 这是 Claude Code 唯一保证每次会话都会加载的文件。`README_ai.md` 是给人读的，`CLAUDE.md` 是给 Claude Code 读的——两者内容相同但后者是强制执行入口。

如果 `CLAUDE.md` 不存在，创建它。如果已存在，在开头插入 "开发规约" 段。格式：

```markdown
# xana Development Rules (CI-enforced)

这些规则在 CI 里由 `tests/test_arch_layers.py` 强制执行。违反 = 构建失败。

1. 永远使用现有接口。加功能前检查 `src/xana/interfaces.py` 和对应模块的 `interfaces.py`。
2. 接口不够 → 先问用户，不要自己加 ABC。用户同意后在 `*/interfaces.py` 加，不能在实现文件里定义新接口。
3. contracts/interfaces/implementation 三层。新模块必须先写 contracts.py → interfaces.py → 再实现。
4. `boot()` 是唯一组合点，禁止在其他文件调用 `register()`。
5. 构造注入，禁止 `from xana.runtime import get_xxx` 全局单例。
6. 每个文件独立可 import，不依赖同目录其他文件。
```

此后任何关于 xana 的会话，Claude Code 都会先读到这些规则。

```python
def test_ext_never_imports_runtime():
    """ext/ 禁止 import harness_sdk.default 或 runtime"""

def test_impl_never_defines_abc():
    """tools/builtin/ 禁止出现 class.*ABC、class.*Protocol"""

def test_no_bus_no_global_singleton():
    """禁止 import bus、禁止模块级实例化 Tool/Skill"""

def test_every_file_independent():
    """ext/**/*.py 每个文件可单独 import，不依赖同目录其他文件"""
```
