#!/usr/bin/env python3
"""Architecture contract checks for xana.

This is the fixed review checklist. It intentionally checks interface shape,
not implementation behavior.
"""
from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "xana"

REQUIRED_MODULES = (
    "agent_adapter", "agents", "component", "config", "harness_sdk", "hooks",
    "llm_driver", "memory", "observability", "policy", "runtime", "skills",
    "state", "tools",
)

REQUIRED_SURFACES = {
    "component": ["ComponentSpec", "LoadOutcome", "SourcePriorityPolicy", "LoadCacheKey"],
    "policy": ["PermissionContext", "PermissionRequest", "ApprovalRequest", "SandboxPolicySpec"],
    "state": ["SessionRecord", "RunRecord", "ResumeDecision", "UsageSnapshot"],
    "hooks": ["HookSpec", "HookMatcher", "HookHandlerRef", "PreToolUseInput"],
    "skills": ["SkillManifest", "SkillPackage", "SkillResource", "SkillScript"],
    "tools": ["ToolSpec", "ToolInvocation", "ToolResult", "ToolArtifact", "ToolDiff"],
    "agents": ["AgentSpec", "SubagentRequest", "SubagentResult", "DelegationPolicy"],
    "agent_adapter": ["CommandSpec", "CommandResult", "DraftHandle", "SessionBinding"],
    "llm_driver": ["ProviderCapabilities", "ProviderError", "RateLimitSignal", "ModelUsage"],
}

REQUIRED_DATA_FLOW_SURFACES = {
    "agent_adapter/contracts.py": ["class InboundMessage", "class RenderedMessage", "class SessionBinding"],
    "agent_adapter/interfaces.py": [
        "def receive(self) -> AsyncIterator[InboundMessage]",
        "def submit(self, inbound: InboundMessage) -> AsyncIterator[RenderedMessage]",
        "def subscribe(self, adapter_id: str, session_id: str) -> AsyncIterator[RenderedMessage]",
    ],
    "agents/contracts.py": ["target_agent: str | None", "class SubagentRequest", "class AgentEvent"],
    "agents/interfaces.py": [
        "def send(self, request: AgentRequest) -> AsyncIterator[AgentEvent]",
        "async def to_harness_request",
        "async def to_harness_context",
        "class SubagentDispatcher",
    ],
    "harness_sdk/contracts.py": [
        "agent_name: str",
        "model_registry:",
        "tool_registry:",
        "skill_registry:",
        "active_context_store:",
        "history_mirror:",
    ],
    "harness_sdk/interfaces.py": [
        "def invoke(self, request: HarnessRequest, context: HarnessContext) -> AsyncIterator[HarnessEvent]",
        "class PromptAssembler",
        "class ModelRequestBuilder",
        "class UsageAccumulator",
        "class ToolCallParser",
        "class ToolDispatcher",
    ],
    "tools/contracts.py": ["session_id: str", "agent_name: str", "run_id: str | None", "class ToolResult"],
    "tools/interfaces.py": ["class ToolContextBuilder", "class Tool", "async def check_permission", "async def cancel"],
    "skills/contracts.py": ["agent_name: str", "run_id: str | None", "class SkillContribution"],
    "skills/interfaces.py": ["class SkillRegistry", "async def discover", "async def activate"],
    "llm_driver/interfaces.py": ["class ModelClient", "def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]"],
    "memory/interfaces.py": ["class ActiveContextStore", "class HistoryMirror"],
    "state/interfaces.py": ["class SessionStore", "class RunStore", "class ResumePolicy"],
}

ALLOWED_RAW_PATTERNS = (
    "raw: object",
    "raw_event: object",
    "raw_response: object",
    "attachments: tuple[dict",
    "metadata: dict",
    "details: dict",
    "args_schema: dict",
    "arg_schema: dict",
    "input_schema: dict",
    "output_schema: dict",
    "tools_schema: tuple[dict",
    "structured_content: dict",
    "env: dict",
    "source_map: dict",
    "config_snapshot: dict",
    "payload: dict",
    "raw: dict",
    "input: dict",
    "args: dict",
)

FORBIDDEN_INTERNAL_RETURNS = (
    "-> dict",
    "-> tuple[dict",
)

errors: list[str] = []


def fail(msg: str) -> None:
    errors.append(msg)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def is_dataclass_decorator(decorator: ast.expr) -> bool:
    if isinstance(decorator, ast.Name):
        return decorator.id == "dataclass"
    if isinstance(decorator, ast.Call):
        func = decorator.func
        return isinstance(func, ast.Name) and func.id == "dataclass"
    return False


# 0. every required module has contracts/interfaces/readme_ai.
for module in REQUIRED_MODULES:
    module_dir = SRC / module
    if not module_dir.exists():
        fail(f"required module missing: {module}")
        continue
    for filename in ("contracts.py", "interfaces.py", "readme_ai.md"):
        if not (module_dir / filename).exists():
            fail(f"required module file missing: {module}/{filename}")

# 1. contracts.py only dataclass / Literal / type alias; no ABC / abstractmethod.
for path in SRC.glob("*/contracts.py"):
    text = path.read_text()
    if re.search(r"\bABC\b|abstractmethod", text):
        fail(f"contracts contains ABC/abstractmethod: {path}")

    mod = parse(path)
    for node in mod.body:
        if isinstance(node, ast.ClassDef):
            if not any(is_dataclass_decorator(d) for d in node.decorator_list):
                fail(f"contract class is not dataclass: {path}:{node.lineno}:{node.name}")


# root contracts.py also should not contain ABC.
root_contracts = SRC / "contracts.py"
if root_contracts.exists():
    text = root_contracts.read_text()
    if re.search(r"\bABC\b|abstractmethod", text):
        fail(f"root contracts contains ABC/abstractmethod: {root_contracts}")

# 2. interfaces.py only ABC classes; all class bases must include ABC or inherit an ABC marker class.
for path in SRC.glob("*/interfaces.py"):
    mod = parse(path)
    for node in mod.body:
        if isinstance(node, ast.ClassDef):
            base_names = {ast.unparse(b) for b in node.bases}
            if "ABC" not in base_names and not base_names.intersection({
                "Hook", "PreTurnHook", "PostTurnHook", "PreModelRequestHook",
                "PostModelResponseHook", "PreToolUseHook", "PostToolUseHook",
            }):
                fail(f"interface class does not inherit ABC: {path}:{node.lineno}:{node.name}")

            for item in node.body:
                if isinstance(item, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    decorators = {ast.unparse(d) for d in item.decorator_list}
                    if "abstractmethod" not in decorators:
                        fail(f"interface method is not abstract: {path}:{item.lineno}:{node.name}.{item.name}")


# 3. public interfaces are abstract.
sys.path.insert(0, str(ROOT / "src"))
import xana.interfaces as public_interfaces  # noqa: E402


public_names = list(public_interfaces.__all__)
if len(public_names) != len(set(public_names)):
    fail("public __all__ contains duplicates")

# every module interface class must be re-exported from xana.interfaces.
for path in SRC.glob("*/interfaces.py"):
    mod = parse(path)
    for node in mod.body:
        if isinstance(node, ast.ClassDef):
            if node.name not in public_names:
                fail(f"interface class not publicly exported: {path}:{node.lineno}:{node.name}")

for name in public_interfaces.__all__:
    cls = getattr(public_interfaces, name)
    if not inspect.isclass(cls):
        fail(f"public export is not class: {name}")
    elif not inspect.isabstract(cls) or not getattr(cls, "__abstractmethods__", None):
        fail(f"public export is not abstract: {name}")

# 4. no known duplicate state authority.
all_text = "\n".join(p.read_text() for p in SRC.rglob("*.py"))
for forbidden in ("AgentSessionStore", "TurnRecord", "TurnUsage"):
    if forbidden in all_text:
        fail(f"duplicate/old authority remains: {forbidden}")

# 5. no turn_id naming.
if "turn_id" in all_text:
    fail("turn_id remains; use run_id")

# 6. no internal naked dict/object returns except allowed raw boundaries.
for path in SRC.rglob("*.py"):
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        stripped = line.strip()
        if any(token in stripped for token in FORBIDDEN_INTERNAL_RETURNS):
            if not any(allowed in stripped for allowed in ALLOWED_RAW_PATTERNS):
                fail(f"naked dict return: {path}:{lineno}: {stripped}")
        if "object" in stripped:
            if not any(allowed in stripped for allowed in ALLOWED_RAW_PATTERNS):
                fail(f"object type outside raw boundary: {path}:{lineno}: {stripped}")

# 7. required CC/Codex surfaces exist.
for module, names in REQUIRED_SURFACES.items():
    contracts = SRC / module / "contracts.py"
    interfaces = SRC / module / "interfaces.py"
    text = ""
    if contracts.exists():
        text += contracts.read_text() + "\n"
    if interfaces.exists():
        text += interfaces.read_text() + "\n"
    for name in names:
        if f"class {name}" not in text and f"{name} =" not in text:
            fail(f"required surface missing: {module}.{name}")

# 8. harness context must have typed interface references, not object placeholders.
harness_contracts = (SRC / "harness_sdk" / "contracts.py").read_text()
for field in (
    "state_repository", "active_context_store", "history_mirror", "tool_registry",
    "skill_registry", "hook_runner", "trace_sink", "progress_sink",
):
    if field not in harness_contracts:
        fail(f"HarnessContext missing field: {field}")
if "object | None" in harness_contracts or ": object" in harness_contracts:
    fail("HarnessContext contains object placeholder")

# 9. tests are run separately; this script only checks architecture shape.

# 9. data-flow contracts: external message must have an explicit path to rendered output,
# and harness must expose the registries/context needed to run model/tool/skill loops.
for relative_path, needles in REQUIRED_DATA_FLOW_SURFACES.items():
    path = SRC / relative_path
    text = path.read_text() if path.exists() else ""
    for needle in needles:
        if needle not in text:
            fail(f"data-flow surface missing: {relative_path}: {needle}")

# 10. tests are run separately; this script only checks architecture shape.

if errors:
    print("ARCH CONTRACT CHECK FAILED")
    for err in errors:
        print(f"- {err}")
    sys.exit(1)

print("ARCH CONTRACT CHECK PASSED")
print(f"public_interfaces={len(public_interfaces.__all__)}")
