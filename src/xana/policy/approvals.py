"""Execution-time approval decisions, scoped grants and pending user interactions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import time
from uuid import uuid4

from xana.harness_sdk.contracts import AbortSignal, HarnessEvent, HarnessEventKind
from xana.harness_sdk.events import RunEvents
from xana.policy.contracts import Approval, ApprovalDecision, PermissionContext, PermissionMode, PermissionResult
from xana.policy.shell import ShellClassifier
from xana.session_config.interfaces import SessionConfigStore
from xana.state.interfaces import SessionStore
from xana.tools.contracts import ToolContext, ToolInvocation, ToolSpec

class PermissionService:
    def __init__(self, *, configs: SessionConfigStore | None = None, sessions: SessionStore | None = None,
                 events: RunEvents | None = None) -> None:
        self._configs = configs
        self._sessions = sessions
        self._events = events
        self._shell = ShellClassifier()
        self._pending: dict[str, tuple[Approval, asyncio.Future[tuple[ApprovalDecision, bool]]]] = {}
        self._resolved: dict[str, Approval] = {}
        self._mode_changed: set[str] = set()
        self._grants: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def current(self, context: PermissionContext) -> tuple[PermissionContext, str]:
        if self._configs is None:
            return context, context.session_id
        sid, root, mode, plan = context.session_id, context.session_id, None, context.plan_mode
        seen = set()
        rank = {"manual": 0, "yolo": 1, "auto": 2}
        while sid:
            if sid in seen:
                raise ValueError("cyclic session ancestry")
            seen.add(sid)
            cfg = await self._configs.get(sid)
            plan |= cfg.metadata.get("mode") == "plan"
            record = await self._sessions.get(sid) if self._sessions is not None else None
            parent = record.parent_session_id if record else None
            selected = cfg.permission_mode or ("yolo" if parent is None else None)
            if selected is not None:
                mode = selected if mode is None else min(mode, selected, key=rank.__getitem__)
            root, sid = sid, parent
        return replace(context, mode=mode or "yolo", plan_mode=plan), root

    async def configure(self, session_id: str, mode: PermissionMode) -> None:
        if mode not in {"manual", "yolo", "auto"}:
            raise ValueError("invalid permission mode")
        if self._configs is None:
            raise RuntimeError("session configuration is required")
        async with self._lock:
            cfg = await self._configs.get(session_id)
            await self._configs.save(replace(cfg, permission_mode=mode))
        self._publish("permission_changed", session_id, "", {"mode": mode})
        # Wake pending calls so they re-evaluate before any execution.
        for approval, future in tuple(self._pending.values()):
            if session_id in {approval.session_id, approval.owner_session_id} and not future.done():
                self._mode_changed.add(approval.id)
                future.set_result(("approved", False))

    def pending(self, session_id: str) -> tuple[Approval, ...]:
        return tuple(deepcopy(approval) for approval, _ in self._pending.values()
                     if session_id in {approval.session_id, approval.owner_session_id})

    async def respond(self, session_id: str, ident: str, decision: ApprovalDecision,
                      *, session_scope: bool = False) -> Approval:
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

    async def authorize(self, invocation: ToolInvocation, spec: ToolSpec, context: ToolContext,
                        base: PermissionResult, abort: AbortSignal) -> PermissionResult:
        if base.decision == "deny":
            return base
        while True:
            permission, root = await self.current(context.permission)
            if permission.plan_mode and not spec.read_only:
                return PermissionResult("deny", "plan mode is read-only")
            if (permission.mode == "auto" and invocation.tool_name == "notify_user"
                    and invocation.input.get("kind") == "decision"):
                return PermissionResult("deny", "decision requests disabled in auto permission mode")
            needs_approval = base.decision == "ask"
            if permission.mode == "manual":
                needs_approval |= not spec.read_only and spec.component.name not in _DEFAULT_APPROVE
            elif permission.mode == "yolo" and spec.component.name == "Bash":
                needs_approval |= self._shell.classify(str(invocation.input.get("command", ""))) == "dangerous"
            if permission.mode == "auto":
                needs_approval = False
            if not needs_approval:
                return PermissionResult("allow")
            fingerprint = _fingerprint(context, invocation)
            if await self._granted(root, fingerprint):
                return PermissionResult("allow")
            decision, scope, changed = await self._request(root, context, invocation, abort)
            if abort.aborted or decision != "approved":
                return PermissionResult("deny", "approval cancelled" if abort.aborted else f"approval {decision}")
            if changed:
                continue
            if scope:
                await self._grant(root, fingerprint)
            # Recheck hard constraints and the current mode after the response.
            latest, _ = await self.current(context.permission)
            if latest.plan_mode and not spec.read_only:
                return PermissionResult("deny", "plan mode is read-only")
            return PermissionResult("allow")

    async def _request(self, root: str, context: ToolContext, invocation: ToolInvocation,
                       abort: AbortSignal) -> tuple[ApprovalDecision, bool, bool]:
        approval = Approval(uuid4().hex, context.session_id, root, context.run_id or "", invocation.call_id,
            invocation.tool_name, deepcopy(invocation.input), context.cwd, time.time())
        future = asyncio.get_running_loop().create_future()
        self._pending[approval.id] = (approval, future)
        self._publish("approval_requested", root, approval.run_id, asdict(approval))
        cancelled = asyncio.create_task(abort.wait())
        try:
            await asyncio.wait({future, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            decision, scope = future.result() if future.done() and not abort.aborted else ("cancelled", False)
            # A mode-change wakeup has no user response; distinguish it explicitly below.
            changed = approval.id in self._mode_changed
            status = "cancelled" if changed else decision
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
        if self._configs is not None:
            return fingerprint in (await self._configs.get(root)).permission_grants
        return fingerprint in self._grants.get(root, set())

    async def _grant(self, root: str, fingerprint: str) -> None:
        if self._configs is None:
            self._grants.setdefault(root, set()).add(fingerprint)
            return
        async with self._lock:
            cfg = await self._configs.get(root)
            await self._configs.save(replace(cfg, permission_grants=tuple(sorted(set(cfg.permission_grants) | {fingerprint}))))

    def _publish(self, kind: HarnessEventKind, session_id: str, run_id: str, payload: dict) -> None:
        if self._events is not None:
            self._events.publish(HarnessEvent(kind, session_id, run_id, payload))
            child = payload.get("session_id")
            if child and child != session_id:
                self._events.publish(HarnessEvent(kind, child, run_id, payload))


_DEFAULT_APPROVE = frozenset({"agent_run", "TodoWrite", "task_list", "task_wait",
    "skill_load", "tool_search", "create_goal", "get_goal", "update_goal", "notify_user"})


def _fingerprint(context: ToolContext, invocation: ToolInvocation) -> str:
    value = [context.session_id, context.cwd, invocation.tool_name, invocation.input]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
