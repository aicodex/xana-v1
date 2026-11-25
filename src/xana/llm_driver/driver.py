"""ModelDriver: composes WireTransport + ProviderProfile + CredentialResolver + Policies + Tape."""
from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import AsyncGenerator, Awaitable, Callable

import httpx

from xana.llm_driver.accumulator import AttemptAccumulator
from xana.llm_driver.contracts import (
    ModelEvent,
    ModelOutcome,
    ModelRequest,
    ModelUsage,
    ProviderCapabilities,
    ProviderError,
    ProviderProfile,
)
from xana.llm_driver.interfaces import CredentialResolver, ModelClient, WireTransport
from xana.llm_driver.policies import PolicyBundle
from xana.llm_driver.registry import InMemoryModelRegistry  # re-exported for callers
from xana.llm_driver.tape_recorder import (
    LlmTapeRecorder,
    compute_prefix_hash,
    compute_request_hash,
    compute_response_hash,
)
from xana.llm_driver.transports.anthropic_messages import AnthropicMessagesTransport
from xana.llm_driver.transports.openai_chat import OpenAIChatTransport

__all__ = ["InMemoryModelRegistry", "ModelDriver", "build_driver", "complete"]


async def _sleep_or_cancel(
    delay_s: float,
    cancel_ev: asyncio.Event,
    sleep_fn: Callable[[float], Awaitable[None]],
) -> bool:
    """Sleep `delay_s` unless `cancel_ev` fires; returns True if cancelled."""
    if cancel_ev.is_set():
        return True
    if delay_s <= 0:
        return False
    sleep_task = asyncio.create_task(sleep_fn(delay_s))
    cancel_task = asyncio.create_task(cancel_ev.wait())
    try:
        _, pending = await asyncio.wait({sleep_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return cancel_ev.is_set()
    finally:
        unfinished = [t for t in (sleep_task, cancel_task) if not t.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)


async def _next_or_cancel(
    ait: AsyncGenerator[ModelEvent, None],
    cancel_ev: asyncio.Event,
) -> tuple[bool, ModelEvent | None, bool]:
    """Advance `ait` racing against `cancel_ev`.

    Returns `(cancelled, event, exhausted)`. Always cancels and awaits the pending
    task inside `finally:` so `aclosing(gen)` never hits `RuntimeError: aclose():
    asynchronous generator is already running` on outer `CancelledError`.
    """
    next_task = asyncio.create_task(ait.__anext__())
    cancel_task = asyncio.create_task(cancel_ev.wait())
    try:
        _, pending = await asyncio.wait({next_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        unfinished = [t for t in (next_task, cancel_task) if not t.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)

    if cancel_ev.is_set():
        if next_task.done() and not next_task.cancelled():
            next_task.exception()  # prevent "Task exception was never retrieved"
        return True, None, False
    try:
        return False, next_task.result(), False
    except StopAsyncIteration:
        return False, None, True


@dataclass
class _FailureAction:
    kind: str  # "continue" | "retry" | "fail"
    next_req: ModelRequest
    delay_s: float = 0.0
    reason_code: str = ""
    auth_refreshed: bool = False


class ModelDriver(ModelClient):
    """Single-request LLM driver with pluggable policies, cancellation, and Tape observability."""

    def __init__(
        self,
        *,
        profile: ProviderProfile,
        transport: WireTransport,
        credentials: CredentialResolver,
        policies: PolicyBundle | None = None,
        recorder: LlmTapeRecorder | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._profile = profile
        self._transport = transport
        self._credentials = credentials
        self._policies = policies or PolicyBundle.default()
        self._recorder = recorder or LlmTapeRecorder(None)
        self._sleep = sleep or asyncio.sleep
        self._active_cancels: dict[str, asyncio.Event] = {}

    @property
    def profile(self) -> ProviderProfile:
        return self._profile

    @property
    def policies(self) -> PolicyBundle:
        return self._policies

    async def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        return self._transport.capabilities(self._profile, model)

    async def cancel(self, request_id: str) -> None:
        ev = self._active_cancels.get(request_id)
        if ev is not None:
            ev.set()

    async def aclose(self) -> None:
        await self._transport.aclose()

    def _build_outcome(
        self,
        *,
        status: str,
        request: ModelRequest,
        current_req: ModelRequest,
        acc: AttemptAccumulator,
        attempt: int,
        ttft_ms: float | None,
        call_started: float,
        wasted_usage: ModelUsage,
        wasted_retry_ms: float,
        error: ProviderError | None = None,
    ) -> ModelOutcome:
        stop_reason = acc.stop_reason or "stop"
        return ModelOutcome(
            status=status,  # type: ignore[arg-type]
            text=acc.text,
            thinking=acc.thinking,
            tool_calls=tuple(acc.tool_calls),
            stop_reason=stop_reason if status == "done" else "",
            usage=acc.usage if status == "done" else ModelUsage(),
            wasted_usage=wasted_usage,
            error=error,
            attempts=attempt,
            ttft_ms=ttft_ms,
            latency_ms=(time.monotonic() - call_started) * 1000.0,
            wasted_retry_ms=wasted_retry_ms,
            request_hash=compute_request_hash(request),
            prefix_hash=compute_prefix_hash(current_req),
            response_hash=(
                compute_response_hash(
                    text=acc.text,
                    thinking=acc.thinking,
                    tool_calls=tuple(acc.tool_calls),
                    stop_reason=stop_reason,
                )
                if status == "done"
                else ""
            ),
            provider=self._profile.name,
            model=current_req.model,
            provider_request_id=acc.provider_request_id,
        )

    async def _decide_failure(
        self,
        *,
        err: ProviderError,
        attempt: int,
        acc: AttemptAccumulator,
        current_req: ModelRequest,
        call_span_id: str,
        auth_refreshed: bool,
        failed_targets: list[tuple[str, str]],
    ) -> _FailureAction:
        # 1. OAuth 401 rotation (once per logical call)
        if err.should_rotate_credential and self._credentials.can_refresh and not auth_refreshed:
            try:
                await self._credentials.auth_headers(force_refresh=True)
                self._recorder.record_policy_decision(
                    call_span_id=call_span_id,
                    req=current_req,
                    policy=self._policies.retry.component_version(),
                    action="rotate_credential",
                    reason_code="auth_401_force_refresh",
                )
                return _FailureAction(kind="continue", next_req=current_req, auth_refreshed=True)
            except Exception as refresh_exc:
                err = ProviderError(
                    kind="auth",
                    message=f"credential refresh failed: {refresh_exc}",
                    retryable=False,
                    reason="auth",
                )

        # 2. Request rewrite on overflow (resolves profile default_max_tokens when request.max_tokens is None)
        if err.reason == "context_overflow" and not acc.has_visible_output and not acc.has_tool_calls:
            eff_req = (
                replace(current_req, max_tokens=self._profile.resolved_max_tokens(current_req.model, None))
                if current_req.max_tokens is None
                else current_req
            )
            rw = self._policies.rewrite.rewrite_on_error(eff_req, err, attempt=attempt)
            if rw is not None:
                self._recorder.record_policy_decision(
                    call_span_id=call_span_id,
                    req=current_req,
                    policy=self._policies.rewrite.component_version(),
                    action="downgrade",
                    reason_code=rw.reason_code,
                    mutated_fields=rw.mutated_fields,
                )
                return _FailureAction(kind="continue", next_req=rw.request, auth_refreshed=auth_refreshed)

        # 3. Retry / mid-stream recovery decision (always respects RetryPolicy caps & shedding)
        retry_dec = self._policies.retry.decide(err, attempt=attempt, request=current_req)
        if acc.has_visible_output or acc.has_tool_calls or acc.partial_bytes > 0:
            rec_dec = self._policies.stream_recovery.decide(
                err,
                attempt=attempt,
                has_visible_output=acc.has_visible_output,
                has_tool_calls=acc.has_tool_calls,
                request=current_req,
            )
            if rec_dec.action == "retry" and retry_dec.should_retry:
                self._recorder.record_policy_decision(
                    call_span_id=call_span_id,
                    req=current_req,
                    policy=self._policies.stream_recovery.component_version(),
                    action="retry",
                    reason_code=rec_dec.reason_code,
                    delay_ms=retry_dec.delay_s * 1000.0,
                )
                return _FailureAction(
                    kind="retry",
                    next_req=current_req,
                    delay_s=retry_dec.delay_s,
                    reason_code=rec_dec.reason_code,
                    auth_refreshed=auth_refreshed,
                )
        elif retry_dec.should_retry:
            self._recorder.record_policy_decision(
                call_span_id=call_span_id,
                req=current_req,
                policy=self._policies.retry.component_version(),
                action="retry",
                reason_code=retry_dec.reason_code,
                delay_ms=retry_dec.delay_s * 1000.0,
            )
            return _FailureAction(
                kind="retry",
                next_req=current_req,
                delay_s=retry_dec.delay_s,
                reason_code=retry_dec.reason_code,
                auth_refreshed=auth_refreshed,
            )

        # 4. Same-profile model fallback via RoutingStrategy
        if err.should_fallback:
            failed_targets.append((current_req.provider, current_req.model))
            route_dec = self._policies.routing.select_target(current_req, failed_targets=tuple(failed_targets))
            if route_dec is not None and route_dec.provider == current_req.provider and route_dec.model != current_req.model:
                self._recorder.record_policy_decision(
                    call_span_id=call_span_id,
                    req=current_req,
                    policy=self._policies.routing.component_version(),
                    action="fallback",
                    reason_code=route_dec.reason_code,
                    from_target=(current_req.provider, current_req.model),
                    to_target=(route_dec.provider, route_dec.model),
                )
                return _FailureAction(
                    kind="retry",
                    next_req=replace(current_req, model=route_dec.model),
                    delay_s=0.0,
                    reason_code=route_dec.reason_code,
                    auth_refreshed=auth_refreshed,
                )

        return _FailureAction(kind="fail", next_req=current_req, auth_refreshed=auth_refreshed)

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        req_key = request.request_id or f"_anon_{id(request)}_{time.monotonic_ns()}"
        cancel_ev = asyncio.Event()
        self._active_cancels[req_key] = cancel_ev
        call_started = time.monotonic()
        call_span_id = self._recorder.record_request(request)
        current_req = request
        wasted_usage = ModelUsage()
        wasted_retry_ms = 0.0
        attempt = 0
        auth_refreshed = False
        failed_targets: list[tuple[str, str]] = []
        attempt_open = False
        att_span_id = ""
        att_started = call_started
        acc = AttemptAccumulator()
        ttft_ms: float | None = None
        response_recorded = False

        try:
            while True:
                attempt += 1
                att_started = time.monotonic()
                att_span_id = self._recorder.record_attempt_start(current_req, attempt=attempt, call_span_id=call_span_id)
                attempt_open = True
                acc = AttemptAccumulator()
                ttft_ms = None
                attempt_error: ProviderError | None = None
                done_ev: ModelEvent | None = None

                if cancel_ev.is_set():
                    attempt_error = ProviderError(kind="cancelled", message="request cancelled", retryable=False, reason="cancelled")
                else:
                    gen = self._transport.stream_attempt(current_req, self._profile, self._credentials)
                    async with aclosing(gen) as events:
                        ait = events.__aiter__()
                        while True:
                            cancelled, ev, exhausted = await _next_or_cancel(ait, cancel_ev)
                            if cancelled:
                                attempt_error = ProviderError(
                                    kind="cancelled", message="request cancelled", retryable=False, reason="cancelled"
                                )
                                break
                            if exhausted or ev is None:
                                break
                            if ttft_ms is None and ev.kind in ("text_delta", "tool_call"):
                                ttft_ms = (time.monotonic() - att_started) * 1000.0
                                self._recorder.record_attempt_first_byte(
                                    attempt=attempt,
                                    attempt_span_id=att_span_id,
                                    ttft_ms=ttft_ms,
                                    http_status=200,
                                    provider_request_id=acc.provider_request_id,
                                    req=current_req,
                                )
                            if ev.kind == "error" and ev.error is not None:
                                attempt_error = ev.error
                                break
                            acc.observe(ev)
                            if ev.kind == "done":
                                done_ev = ev
                                break
                            yield ev

                elapsed_ms = (time.monotonic() - att_started) * 1000.0
                if done_ev is not None and attempt_error is None:
                    self._recorder.record_attempt_end(
                        attempt=attempt,
                        attempt_span_id=att_span_id,
                        req=current_req,
                        stop_reason=acc.stop_reason or "stop",
                        ttft_ms=ttft_ms,
                        stream_duration_ms=elapsed_ms,
                        usage=acc.usage,
                        output_chars=len(acc.text),
                        tool_call_count=len(acc.tool_calls),
                        malformed_tool_calls=acc.malformed_tool_calls,
                        provider_request_id=acc.provider_request_id,
                    )
                    attempt_open = False
                    outcome = self._build_outcome(
                        status="done",
                        request=request,
                        current_req=current_req,
                        acc=acc,
                        attempt=attempt,
                        ttft_ms=ttft_ms,
                        call_started=call_started,
                        wasted_usage=wasted_usage,
                        wasted_retry_ms=wasted_retry_ms,
                    )
                    self._recorder.record_response(call_span_id=call_span_id, req=current_req, outcome=outcome)
                    response_recorded = True
                    yield done_ev
                    return

                err = attempt_error or ProviderError(
                    kind="unknown", message="stream terminated unexpectedly", retryable=False, reason="unknown"
                )
                self._recorder.record_attempt_error(
                    attempt=attempt,
                    attempt_span_id=att_span_id,
                    req=current_req,
                    error=err,
                    elapsed_ms=elapsed_ms,
                    partial_bytes=acc.partial_bytes,
                )
                attempt_open = False
                wasted_usage = wasted_usage.add(acc.usage)
                wasted_retry_ms += elapsed_ms

                if err.reason != "cancelled":
                    action = await self._decide_failure(
                        err=err,
                        attempt=attempt,
                        acc=acc,
                        current_req=current_req,
                        call_span_id=call_span_id,
                        auth_refreshed=auth_refreshed,
                        failed_targets=failed_targets,
                    )
                    auth_refreshed = action.auth_refreshed
                    current_req = action.next_req
                    if action.kind == "continue":
                        continue
                    if action.kind == "retry":
                        yield ModelEvent(
                            kind="retry",
                            payload={
                                "attempt": attempt,
                                "reason": err.message,
                                "reason_code": action.reason_code,
                                "delay_s": action.delay_s,
                            },
                            error=err,
                        )
                        if await _sleep_or_cancel(action.delay_s, cancel_ev, self._sleep):
                            err = ProviderError(kind="cancelled", message="request cancelled", retryable=False, reason="cancelled")
                        else:
                            wasted_retry_ms += action.delay_s * 1000.0
                            continue

                outcome = self._build_outcome(
                    status="aborted" if err.reason == "cancelled" else "failed",
                    request=request,
                    current_req=current_req,
                    acc=acc,
                    attempt=attempt,
                    ttft_ms=ttft_ms,
                    call_started=call_started,
                    wasted_usage=wasted_usage,
                    wasted_retry_ms=wasted_retry_ms,
                    error=err,
                )
                self._recorder.record_response(call_span_id=call_span_id, req=current_req, outcome=outcome)
                response_recorded = True
                yield ModelEvent(kind="error", error=err)
                return
        except (asyncio.CancelledError, GeneratorExit):
            if not response_recorded:
                abort_err = ProviderError(kind="cancelled", message="stream aborted", retryable=False, reason="cancelled")
                if attempt_open:
                    self._recorder.record_attempt_error(
                        attempt=attempt,
                        attempt_span_id=att_span_id,
                        req=current_req,
                        error=abort_err,
                        elapsed_ms=(time.monotonic() - att_started) * 1000.0,
                        partial_bytes=acc.partial_bytes,
                    )
                outcome = self._build_outcome(
                    status="aborted",
                    request=request,
                    current_req=current_req,
                    acc=acc,
                    attempt=max(1, attempt),
                    ttft_ms=ttft_ms,
                    call_started=call_started,
                    wasted_usage=wasted_usage,
                    wasted_retry_ms=wasted_retry_ms,
                    error=abort_err,
                )
                self._recorder.record_response(call_span_id=call_span_id, req=current_req, outcome=outcome)
            raise
        finally:
            self._active_cancels.pop(req_key, None)


def build_driver(
    profile: ProviderProfile,
    *,
    credentials: CredentialResolver,
    transport: WireTransport | None = None,
    policies: PolicyBundle | None = None,
    recorder: LlmTapeRecorder | None = None,
    http_client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> ModelDriver:
    """Assemble a ModelDriver from a declarative ProviderProfile and CredentialResolver."""
    if transport is None:
        if profile.dialect == "openai_chat":
            transport = OpenAIChatTransport(http_client=http_client)
        elif profile.dialect == "anthropic_messages":
            transport = AnthropicMessagesTransport(http_client=http_client)
        else:
            raise ValueError(f"unsupported wire dialect: {profile.dialect!r}")
    return ModelDriver(
        profile=profile,
        transport=transport,
        credentials=credentials,
        policies=policies,
        recorder=recorder,
        sleep=sleep,
    )


async def complete(client: ModelClient, request: ModelRequest) -> ModelOutcome:
    """Consume a ModelClient stream to completion and return a settled ModelOutcome."""
    started = time.monotonic()
    attempt_started = started
    acc = AttemptAccumulator()
    ttft_ms: float | None = None
    attempts = 1
    saw_done = False
    last_error: ProviderError | None = None

    async with aclosing(client.stream(request)) as events:
        async for ev in events:
            if ev.kind == "retry":
                attempts += 1
                acc = AttemptAccumulator()
                ttft_ms = None
                attempt_started = time.monotonic()
                continue
            if ttft_ms is None and ev.kind in ("text_delta", "tool_call"):
                ttft_ms = (time.monotonic() - attempt_started) * 1000.0
            if ev.kind == "error":
                last_error = ev.error or ProviderError(kind="unknown", message="unknown error", retryable=False)
                break
            if ev.kind == "rate_limited":
                rl = ev.rate_limit
                last_error = ProviderError(
                    kind="rate_limit",
                    message=(rl.reason if rl and rl.reason else "rate limited"),
                    retryable=True,
                    reason="rate_limit",
                    retry_after_s=rl.retry_after_s if rl else None,
                )
                break
            acc.observe(ev)
            if ev.kind == "done":
                saw_done = True
                break

    elapsed_ms = (time.monotonic() - started) * 1000.0
    if last_error is None and not saw_done:
        last_error = ProviderError(
            kind="network",
            message="stream ended without done event",
            retryable=True,
            reason="stream_eof",
        )
    if last_error is not None:
        return ModelOutcome(
            status="aborted" if last_error.reason == "cancelled" else "failed",
            text=acc.text,
            thinking=acc.thinking,
            tool_calls=tuple(acc.tool_calls),
            usage=acc.usage,
            error=last_error,
            attempts=attempts,
            ttft_ms=ttft_ms,
            latency_ms=elapsed_ms,
            request_hash=compute_request_hash(request),
            prefix_hash=compute_prefix_hash(request),
            provider=request.provider,
            model=request.model,
        )
    stop_reason = acc.stop_reason or "stop"
    return ModelOutcome(
        status="done",
        text=acc.text,
        thinking=acc.thinking,
        tool_calls=tuple(acc.tool_calls),
        stop_reason=stop_reason,
        usage=acc.usage,
        attempts=attempts,
        ttft_ms=ttft_ms,
        latency_ms=elapsed_ms,
        request_hash=compute_request_hash(request),
        prefix_hash=compute_prefix_hash(request),
        response_hash=compute_response_hash(
            text=acc.text,
            thinking=acc.thinking,
            tool_calls=tuple(acc.tool_calls),
            stop_reason=stop_reason,
        ),
        provider=request.provider,
        model=request.model,
        provider_request_id=acc.provider_request_id,
    )
