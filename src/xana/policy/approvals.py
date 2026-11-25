"""Execution-time approval decisions, scoped grants, and pending user interactions.

Strictly decoupled from harness_sdk, session_config, state, and tools:
uses PolicyStatePort, PolicyEventSink, CommandRiskClassifier, and AuthorizationRequest.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any
from uuid import uuid4

from xana.policy.contracts import (
    Approval,
    ApprovalDecision,
    AuthorizationRequest,
    PermissionContext,
    PermissionMode,
    PermissionResult,
)
from xana.policy.interfaces import (
    AbortLike,
    CommandRiskClassifier,
    PolicyEventSink,
    PolicyStatePort,
)
from xana.policy.shell import TreeSitterShellClassifier
from xana.policy.tape_recorder import PolicyTapeRecorder
from xana.protocol.diag import DiagLogger

_DIAG = DiagLogger("policy")

_DEFAULT_APPROVE = frozenset({
    "agent_run", "TodoWrite", "task_list", "task_wait",
    "skill_load", "tool_search", "create_goal", "get_goal", "update_goal", "notify_user",
})


class PermissionService:
    """Authorizes tool invocations under manual / yolo / auto modes, plan mode, and ancestor caps."""

    def __init__(
        self,
        *,
        state: PolicyStatePort | None = None,
        events: PolicyEventSink | None = None,
        classifier: CommandRiskClassifier | None = None,
        recorder: PolicyTapeRecorder | None = None,
    ) -> None:
        self._state = state
        self._events = events
        self._classifier: CommandRiskClassifier = classifier or TreeSitterShellClassifier()
        self._recorder = recorder or PolicyTapeRecorder(None)
        self._pending: dict[str, tuple[Approval, asyncio.Future[tuple[ApprovalDecision, bool]]]] = {}
        self._resolved: dict[str, Approval] = {}
        self._mode_changed: set[str] = set()
        self._grants: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def current(self, context: PermissionContext) -> tuple[PermissionContext, str]:
        if self._state is None:
            return context, context.session_id
        sid, root, mode, plan = context.session_id, context.session_id, None, context.plan_mode
        seen: set[str] = set()
        rank = {"manual": 0, "yolo": 1, "auto": 2}
        while sid:
            if sid in seen:
                raise ValueError("cyclic session ancestry")
            seen.add(sid)
            snap = await self._state.get_session_policy(sid)
            plan |= snap.plan_mode
            parent = snap.parent_session_id
            selected = snap.permission_mode or ("yolo" if parent is None else None)
            if selected is not None:
                mode = selected if mode is None else min(mode, selected, key=rank.__getitem__)
            root, sid = sid, parent
        return replace(context, mode=mode or "yolo", plan_mode=plan), root

    async def configure(self, session_id: str, mode: PermissionMode) -> None:
        if mode not in {"manual", "yolo", "auto"}:
            raise ValueError("invalid permission mode")
        if self._state is None:
            raise RuntimeError("policy state port is required")
        async with self._lock:
            await self._state.save_permission_mode(session_id, mode)
        self._publish("permission_changed", session_id, "", {"mode": mode})
        _DIAG.op("mode.changed", session_id=session_id, mode=mode)
        for approval, future in tuple(self._pending.values()):
            if session_id in {approval.session_id, approval.owner_session_id} and not future.done():
                self._mode_changed.add(approval.id)
                future.set_result(("approved", False))

    def pending(self, session_id: str) -> tuple[Approval, ...]:
        return tuple(
            deepcopy(approval)
            for approval, _ in self._pending.values()
            if session_id in {approval.session_id, approval.owner_session_id}
        )

    async def respond(
        self,
        session_id: str,
        ident: str,
        decision: ApprovalDecision,
        *,
        session_scope: bool = False,
    ) -> Approval:
        if decision not in {"approved", "rejected", "cancelled"}:
            raise ValueError("invalid approval decision")
        pair = self._pending.get(ident)
        approval = pair[0] if pair else self._resolved.get(ident)
        if approval is None or session_id not in {approval.session_id, approval.owner_session_id}:
            raise LookupError("approval not found")
        if pair is None or pair[1].done():
            raise ValueError("approval has ended")
        pair[1].set_result((decision, session_scope))
        return replace(deepcopy(approval), status=decision)

    async def authorize(
        self,
        req: AuthorizationRequest,
        base: PermissionResult,
        abort: AbortLike,
    ) -> PermissionResult:
        t0 = time.monotonic()
        if base.decision == "deny":
            self._finish_record(req, base, mode=req.permission.mode, plan=req.permission.plan_mode, risk=None, t0=t0)
            return base
        while True:
            permission, root = await self.current(req.permission)
            if permission.plan_mode and not req.read_only:
                res = PermissionResult("deny", "plan mode is read-only")
                self._finish_record(req, res, mode=permission.mode, plan=permission.plan_mode, risk=None, t0=t0)
                return res
            if (
                permission.mode == "auto"
                and req.tool_name == "notify_user"
                and req.arguments.get("kind") == "decision"
            ):
                res = PermissionResult("deny", "decision requests disabled in auto permission mode")
                self._finish_record(req, res, mode=permission.mode, plan=permission.plan_mode, risk=None, t0=t0)
                return res

            needs_approval = base.decision == "ask"
            shell_risk: str | None = None
            if permission.mode == "manual":
                needs_approval |= not req.read_only and req.tool_name not in _DEFAULT_APPROVE
            elif permission.mode == "yolo" and req.tool_name == "Bash":
                assessment = self._classifier.classify(str(req.arguments.get("command", "")))
                shell_risk = assessment.risk
                if assessment.risk == "dangerous":
                    needs_approval = True
                    _DIAG.anomaly("shell.dangerous", session_id=req.session_id, tool=req.tool_name)
            if permission.mode == "auto":
                needs_approval = False

            if not needs_approval:
                res = PermissionResult("allow")
                self._finish_record(req, res, mode=permission.mode, plan=permission.plan_mode, risk=shell_risk, t0=t0)
                return res

            fingerprint = _fingerprint(req)
            if await self._granted(root, fingerprint):
                res = PermissionResult("allow")
                self._finish_record(req, res, mode=permission.mode, plan=permission.plan_mode, risk=shell_risk, t0=t0)
                return res

            decision, scope, changed = await self._request(root, req, abort)
            if abort.aborted or decision != "approved":
                res = PermissionResult("deny", "approval cancelled" if abort.aborted else f"approval {decision}")
                self._finish_record(req, res, mode=permission.mode, plan=permission.plan_mode, risk=shell_risk, t0=t0)
                return res
            if changed:
                continue
            if scope:
                await self._grant(root, fingerprint)
            latest, _ = await self.current(req.permission)
            if latest.plan_mode and not req.read_only:
                res = PermissionResult("deny", "plan mode is read-only")
                self._finish_record(req, res, mode=latest.mode, plan=latest.plan_mode, risk=shell_risk, t0=t0)
                return res
            res = PermissionResult("allow")
            self._finish_record(req, res, mode=latest.mode, plan=latest.plan_mode, risk=shell_risk, t0=t0)
            return res

    def _finish_record(
        self,
        req: AuthorizationRequest,
        result: PermissionResult,
        *,
        mode: str,
        plan: bool,
        risk: str | None,
        t0: float,
    ) -> None:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        self._recorder.record_decision(
            req,
            result,
            effective_mode=mode,
            plan_mode=plan,
            risk=risk,
            classifier=self._classifier.component_version() if risk is not None else None,
            latency_ms=elapsed_ms,
        )
        if result.decision == "deny":
            _DIAG.anomaly("authorize.deny", tool=req.tool_name, mode=mode, plan=plan, reason=result.reason)
        else:
            _DIAG.perf("authorize.allow", latency_ms=elapsed_ms, slow_ms=200.0, tool=req.tool_name, mode=mode)

    async def _request(
        self,
        root: str,
        req: AuthorizationRequest,
        abort: AbortLike,
    ) -> tuple[ApprovalDecision, bool, bool]:
        approval = Approval(
            uuid4().hex,
            req.session_id,
            root,
            req.run_id,
            req.call_id,
            req.tool_name,
            deepcopy(req.arguments),
            req.cwd,
            time.time(),
        )
        future: asyncio.Future[tuple[ApprovalDecision, bool]] = asyncio.get_running_loop().create_future()
        self._pending[approval.id] = (approval, future)
        self._publish("approval_requested", root, approval.run_id, asdict(approval))
        cancelled = asyncio.create_task(abort.wait())
        try:
            await asyncio.wait({future, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            decision, scope = future.result() if future.done() and not abort.aborted else ("cancelled", False)
            changed = approval.id in self._mode_changed
            status: ApprovalDecision = "cancelled" if changed else decision
            resolved = replace(approval, status=status)
            self._resolved[approval.id] = resolved
            while len(self._resolved) > 256:
                self._resolved.pop(next(iter(self._resolved)))
            self._publish("approval_resolved", root, approval.run_id, asdict(resolved))
            return decision, scope, changed
        except BaseException:
            self._resolved[approval.id] = replace(approval, status="cancelled")
            self._publish("approval_resolved", root, approval.run_id, asdict(self._resolved[approval.id]))
            raise
        finally:
            self._pending.pop(approval.id, None)
            self._mode_changed.discard(approval.id)
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            if not future.done():
                future.cancel()

    async def _granted(self, root: str, fingerprint: str) -> bool:
        if self._state is not None:
            return fingerprint in (await self._state.get_session_policy(root)).permission_grants
        return fingerprint in self._grants.get(root, set())

    async def _grant(self, root: str, fingerprint: str) -> None:
        if self._state is None:
            self._grants.setdefault(root, set()).add(fingerprint)
            return
        async with self._lock:
            await self._state.add_permission_grant(root, fingerprint)

    def _publish(self, kind: str, session_id: str, run_id: str, payload: dict[str, Any]) -> None:
        if self._events is not None:
            self._events.publish(kind, session_id, run_id, payload)
            child = payload.get("session_id")
            if child and child != session_id:
                self._events.publish(kind, str(child), run_id, payload)


def _fingerprint(req: AuthorizationRequest) -> str:
    value = [req.session_id, req.cwd, req.tool_name, req.arguments]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
