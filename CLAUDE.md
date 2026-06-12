# xana Development Rules (CI-enforced)

这些规则在 CI 里由 `tests/test_arch_layers.py` 强制执行。违反 = 构建失败。

1. 永远使用现有接口。加功能前检查 `src/xana/interfaces.py` 和对应模块的 `interfaces.py`。
2. 接口不够 → 先问用户，不要自己加 ABC。用户同意后在 `*/interfaces.py` 加，不能在实现文件里定义新接口。
3. contracts/interfaces/implementation 三层。新模块必须先写 contracts.py → interfaces.py → 再实现。
4. `boot()` 是唯一组合点，禁止在其他文件调用 `register()`。
5. 构造注入，禁止 `from xana.runtime import get_xxx` 全局单例。
6. 每个文件独立可 import，不依赖同目录其他文件。

## Extension API（src/xana/extension_api/）

- 8 种 Contributor ABC 在 `extension_api/interfaces.py`；数据契约在 `extension_api/contracts.py`。
- Extension 通过 `ExtensionRegistryBuilder().add(ext).build()` 组装，frozen registry 由 `boot(extension_registry=...)` 注入 `DefaultHarness`。
- `extension_registry=None` 时 harness 行为与未引入 Extension API 前完全一致。
- Runtime Service ABC（SchedulerService / SandboxManager / AgentSpawner）在 `extension_api/capabilities.py`，构造注入给 Extension。
