"""Standalone execution-environment & permission policy subsystem."""
from xana.policy.approvals import PermissionService
from xana.policy.contracts import (
    POLICY_SCHEMA_VERSION,
    Approval,
    ApprovalDecision,
    AuthorizationRequest,
    PathOperation,
    PermissionContext,
    PermissionDecision,
    PermissionMode,
    PermissionResult,
    ProcessOutcome,
    ProcessSpec,
    RiskAssessment,
    SandboxMode,
    SandboxPolicySpec,
    SessionPolicySnapshot,
    ShellRisk,
)
from xana.policy.exec_env import LocalProcessEnvironment
from xana.policy.interfaces import (
    AbortLike,
    CommandRiskClassifier,
    ExecutionEnvironment,
    PathBoundaryPolicy,
    PolicyEventSink,
    PolicyStatePort,
)
from xana.policy.path_policy import ProtectedDirsPathPolicy, resolve_target_path
from xana.policy.shell import ShellClassifier, TreeSitterShellClassifier
from xana.policy.tape_recorder import PolicyTapeRecorder
from xana.policy.testing import FakeExecutionEnvironment, InMemoryPolicyState, RecordingPolicyEventSink

__all__ = [
    "AbortLike",
    "Approval",
    "ApprovalDecision",
    "AuthorizationRequest",
    "CommandRiskClassifier",
    "ExecutionEnvironment",
    "FakeExecutionEnvironment",
    "InMemoryPolicyState",
    "LocalProcessEnvironment",
    "POLICY_SCHEMA_VERSION",
    "PathBoundaryPolicy",
    "PathOperation",
    "PermissionContext",
    "PermissionDecision",
    "PermissionMode",
    "PermissionResult",
    "PermissionService",
    "PolicyEventSink",
    "PolicyStatePort",
    "PolicyTapeRecorder",
    "ProcessOutcome",
    "ProcessSpec",
    "ProtectedDirsPathPolicy",
    "RecordingPolicyEventSink",
    "RiskAssessment",
    "SandboxMode",
    "SandboxPolicySpec",
    "SessionPolicySnapshot",
    "ShellClassifier",
    "ShellRisk",
    "TreeSitterShellClassifier",
    "resolve_target_path",
]
