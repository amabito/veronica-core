"""VERONICA Runtime hooks — RuntimeContext for instrumenting LLM/tool calls."""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Generator

from veronica.runtime.events import Event, EventBus, EventTypes, make_event
from veronica.runtime.models import (
    Budget,
    Labels,
    Run,
    RunStatus,
    Session,
    SessionCounters,
    SessionStatus,
    Severity,
    Step,
    StepError,
    StepKind,
    StepStatus,
    generate_uuidv7,
    now_iso,
)
from veronica.runtime.sinks import EventSink, create_default_sinks
from veronica.runtime.state_machine import (
    transition_run,
    transition_session,
)
from veronica.budget.enforcer import BudgetEnforcer, BudgetExceeded
from veronica.scheduler.types import (
    AdmitResult,
    Priority,
    QueueEntry,
    SchedulerQueued,
    SchedulerRejected,
)
from veronica.control.decision import DegradedToolBlocked, DegradedRejected, ControlSignals, RequestMeta
from veronica.control.controller import DegradeController


class MaxStepsExceeded(Exception):
    """Raised when a session has reached its configured max_steps limit."""

    def __init__(self, session_id: str, steps_executed: int, max_steps: int) -> None:
        self.session_id = session_id
        self.steps_executed = steps_executed
        self.max_steps = max_steps
        super().__init__(
            f"Session {session_id} exceeded max_steps: {steps_executed}/{max_steps}"
        )


class RuntimeContext:
    """Central context for VERONICA runtime. Manages Run/Session/Step lifecycle and event emission."""

    def __init__(self, sinks: list[EventSink] | None = None, scheduler: "Scheduler | None" = None, enforcer: "BudgetEnforcer | None" = None, controller: "DegradeController | None" = None) -> None:
        actual_sinks = sinks if sinks is not None else create_default_sinks()
        self._bus = EventBus(actual_sinks)
        self._scheduler = scheduler
        self._enforcer = enforcer
        self._controller = controller

    @property
    def bus(self) -> EventBus:
        return self._bus

    # --- Run lifecycle ---

    def create_run(
        self,
        labels: Labels | None = None,
        budget: Budget | None = None,
    ) -> Run:
        run = Run(
            labels=labels or Labels(),
            budget=budget or Budget(),
        )
        self._bus.emit(make_event(
            EventTypes.RUN_CREATED,
            run.run_id,
            labels=run.labels,
            payload={"budget": asdict(run.budget)},
        ))
        return run

    def finish_run(
        self,
        run: Run,
        status: RunStatus = RunStatus.SUCCEEDED,
        error_summary: str | None = None,
    ) -> Run:
        old_status = run.status
        transition_run(run, status, reason=error_summary or "")
        self._bus.emit(make_event(
            EventTypes.RUN_STATE_CHANGED,
            run.run_id,
            labels=run.labels,
            payload={"from": old_status.value, "to": status.value, "reason": error_summary or ""},
        ))
        self._bus.emit(make_event(
            EventTypes.RUN_FINISHED,
            run.run_id,
            labels=run.labels,
            severity=Severity.ERROR if status == RunStatus.FAILED else Severity.INFO,
            payload={
                "status": status.value,
                "error_summary": error_summary,
                "budget_used_usd": run.budget.used_usd,
                "budget_used_tokens": run.budget.used_tokens,
            },
        ))
        return run

    # --- Session lifecycle ---

    def create_session(
        self,
        run: Run,
        agent_name: str = "",
        max_steps: int = 100,
        loop_detection_on: bool = True,
    ) -> Session:
        session = Session(
            run_id=run.run_id,
            agent_name=agent_name,
            max_steps=max_steps,
            loop_detection_on=loop_detection_on,
        )
        self._bus.emit(make_event(
            EventTypes.SESSION_CREATED,
            run.run_id,
            session_id=session.session_id,
            labels=run.labels,
            payload={"agent_name": agent_name, "max_steps": max_steps},
        ))
        return session

    def finish_session(
        self,
        session: Session,
        status: SessionStatus = SessionStatus.SUCCEEDED,
        labels: Labels | None = None,
    ) -> Session:
        transition_session(session, status)
        self._bus.emit(make_event(
            EventTypes.SESSION_FINISHED,
            session.run_id,
            session_id=session.session_id,
            labels=labels or Labels(),
            payload={
                "status": status.value,
                "counters": asdict(session.counters),
            },
        ))
        return session

    # --- Step hooks (context managers) ---

    @contextmanager
    def llm_call(
        self,
        session: Session,
        model: str = "",
        provider: str = "",
        parent_step_id: str | None = None,
        labels: Labels | None = None,
        priority: str = "P1",
        run: Run | None = None,
        cheap_model: str = "",
        max_tokens: int = 4096,
    ) -> Generator[Step, None, None]:
        lbl = labels or Labels()
        self._check_max_steps(session, lbl)
        reserved_usd = 0.0
        if self._enforcer:
            reserved_usd = self._enforcer.pre_check_and_reserve(
                run_id=session.run_id, labels=lbl, kind="llm_call",
            )
        if self._controller:
            signals = self._build_signals(lbl)
            request_meta = RequestMeta(kind="llm_call", priority=priority, model=model,
                cheap_model=cheap_model, max_tokens=max_tokens)
            decision = self._controller.evaluate(lbl.org, lbl.team, signals, request_meta, session.run_id)
            if not decision.allow_llm:
                if self._enforcer:
                    self._enforcer.release_reservation(lbl, reserved_usd)
                raise DegradedRejected(priority, decision.level)
            if decision.model_override:
                model = decision.model_override
            if decision.max_tokens_cap:
                max_tokens = decision.max_tokens_cap
        # Scheduler gate
        if self._scheduler:
            entry = QueueEntry(
                step_id=generate_uuidv7(),
                run_id=session.run_id,
                session_id=session.session_id,
                org=lbl.org,
                team=lbl.team,
                priority=Priority(priority),
                kind="llm_call",
                model=model,
            )
            result = self._scheduler.admit(entry)
            if result == AdmitResult.QUEUE:
                if self._enforcer:
                    self._enforcer.release_reservation(lbl, reserved_usd)
                raise SchedulerQueued(entry.step_id, "inflight_limit")
            elif result == AdmitResult.REJECT:
                if self._enforcer:
                    self._enforcer.release_reservation(lbl, reserved_usd)
                raise SchedulerRejected(entry.step_id, "queue_full")
        step = Step(
            session_id=session.session_id,
            run_id=session.run_id,
            parent_step_id=parent_step_id,
            kind=StepKind.LLM_CALL,
            model=model,
            provider=provider,
        )
        self._bus.emit(make_event(
            EventTypes.STEP_STARTED, session.run_id,
            session_id=session.session_id, step_id=step.step_id,
            parent_step_id=parent_step_id, labels=lbl,
            payload={"kind": step.kind.value, "model": model, "provider": provider},
        ))
        self._bus.emit(make_event(
            EventTypes.LLM_CALL_STARTED, session.run_id,
            session_id=session.session_id, step_id=step.step_id,
            parent_step_id=parent_step_id, labels=lbl,
            payload={"model": model, "provider": provider},
        ))
        start_time = time.monotonic()
        try:
            yield step
            step.finished_at = now_iso()
            step.latency_ms = (time.monotonic() - start_time) * 1000
            step.status = StepStatus.SUCCEEDED
            session.counters.llm_calls += 1
            session.counters.steps_total += 1
            if self._controller:
                self._controller.feed_result(lbl.org, lbl.team, success=True)
            if step.cost_usd:
                session_run_budget = None  # caller updates budget externally
            self._bus.emit(make_event(
                EventTypes.STEP_SUCCEEDED, session.run_id,
                session_id=session.session_id, step_id=step.step_id,
                parent_step_id=parent_step_id, labels=lbl,
                payload={
                    "kind": step.kind.value, "model": model,
                    "latency_ms": step.latency_ms,
                    "tokens_in": step.tokens_in, "tokens_out": step.tokens_out,
                    "cost_usd": step.cost_usd, "result_ref": step.result_ref,
                },
            ))
            self._bus.emit(make_event(
                EventTypes.LLM_CALL_SUCCEEDED, session.run_id,
                session_id=session.session_id, step_id=step.step_id,
                parent_step_id=parent_step_id, labels=lbl,
                payload={
                    "model": model, "latency_ms": step.latency_ms,
                    "tokens_in": step.tokens_in, "tokens_out": step.tokens_out,
                    "cost_usd": step.cost_usd,
                },
            ))
            if self._enforcer and run:
                self._enforcer.post_charge(run, lbl, reserved_usd, step.cost_usd)
            if self._scheduler:
                self._scheduler.release(lbl.org, lbl.team)
        except Exception as exc:
            step.finished_at = now_iso()
            step.latency_ms = (time.monotonic() - start_time) * 1000
            step.status = StepStatus.FAILED
            step.error = StepError(
                type=type(exc).__name__, message=str(exc)[:500],
                retryable=False, classified_reason="",
            )
            session.counters.steps_total += 1
            if self._controller:
                self._controller.feed_result(lbl.org, lbl.team, success=False)
            self._bus.emit(make_event(
                EventTypes.STEP_FAILED, session.run_id,
                session_id=session.session_id, step_id=step.step_id,
                parent_step_id=parent_step_id, labels=lbl,
                severity=Severity.ERROR,
                payload={
                    "kind": step.kind.value, "model": model,
                    "latency_ms": step.latency_ms,
                    "error_type": type(exc).__name__, "error_message": str(exc)[:500],
                },
            ))
            self._bus.emit(make_event(
                EventTypes.LLM_CALL_FAILED, session.run_id,
                session_id=session.session_id, step_id=step.step_id,
                parent_step_id=parent_step_id, labels=lbl,
                severity=Severity.ERROR,
                payload={
                    "model": model, "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                },
            ))
            if self._enforcer and run:
                self._enforcer.post_charge(run, lbl, reserved_usd, step.cost_usd)
            if self._scheduler:
                self._scheduler.release(lbl.org, lbl.team)
            raise

    @contextmanager
    def tool_call(
        self,
        session: Session,
        tool_name: str = "",
        parent_step_id: str | None = None,
        labels: Labels | None = None,
        priority: str = "P1",
        run: Run | None = None,
        read_only_tools: frozenset[str] | None = None,
    ) -> Generator[Step, None, None]:
        lbl = labels or Labels()
        self._check_max_steps(session, lbl)
        reserved_usd = 0.0
        if self._enforcer:
            reserved_usd = self._enforcer.pre_check_and_reserve(
                run_id=session.run_id, labels=lbl, kind="tool_call",
            )
        if self._controller:
            signals = self._build_signals(lbl)
            request_meta = RequestMeta(kind="tool_call", priority=priority, tool_name=tool_name,
                read_only_tools=read_only_tools or frozenset())
            decision = self._controller.evaluate(lbl.org, lbl.team, signals, request_meta, session.run_id)
            if not decision.allow_tools:
                if self._enforcer:
                    self._enforcer.release_reservation(lbl, reserved_usd)
                raise DegradedToolBlocked(tool_name, decision.level)
        # Scheduler gate
        if self._scheduler:
            entry = QueueEntry(
                step_id=generate_uuidv7(),
                run_id=session.run_id,
                session_id=session.session_id,
                org=lbl.org,
                team=lbl.team,
                priority=Priority(priority),
                kind="tool_call",
                model="",
            )
            result = self._scheduler.admit(entry)
            if result == AdmitResult.QUEUE:
                if self._enforcer:
                    self._enforcer.release_reservation(lbl, reserved_usd)
                raise SchedulerQueued(entry.step_id, "inflight_limit")
            elif result == AdmitResult.REJECT:
                if self._enforcer:
                    self._enforcer.release_reservation(lbl, reserved_usd)
                raise SchedulerRejected(entry.step_id, "queue_full")
        step = Step(
            session_id=session.session_id,
            run_id=session.run_id,
            parent_step_id=parent_step_id,
            kind=StepKind.TOOL_CALL,
            tool=tool_name,
        )
        self._bus.emit(make_event(
            EventTypes.STEP_STARTED, session.run_id,
            session_id=session.session_id, step_id=step.step_id,
            parent_step_id=parent_step_id, labels=lbl,
            payload={"kind": step.kind.value, "tool": tool_name},
        ))
        self._bus.emit(make_event(
            EventTypes.TOOL_CALL_STARTED, session.run_id,
            session_id=session.session_id, step_id=step.step_id,
            parent_step_id=parent_step_id, labels=lbl,
            payload={"tool": tool_name},
        ))
        start_time = time.monotonic()
        try:
            yield step
            step.finished_at = now_iso()
            step.latency_ms = (time.monotonic() - start_time) * 1000
            step.status = StepStatus.SUCCEEDED
            session.counters.tool_calls += 1
            session.counters.steps_total += 1
            if self._controller:
                self._controller.feed_result(lbl.org, lbl.team, success=True)
            self._bus.emit(make_event(
                EventTypes.STEP_SUCCEEDED, session.run_id,
                session_id=session.session_id, step_id=step.step_id,
                parent_step_id=parent_step_id, labels=lbl,
                payload={
                    "kind": step.kind.value, "tool": tool_name,
                    "latency_ms": step.latency_ms, "result_ref": step.result_ref,
                },
            ))
            self._bus.emit(make_event(
                EventTypes.TOOL_CALL_SUCCEEDED, session.run_id,
                session_id=session.session_id, step_id=step.step_id,
                parent_step_id=parent_step_id, labels=lbl,
                payload={"tool": tool_name, "latency_ms": step.latency_ms},
            ))
            if self._enforcer and run:
                self._enforcer.post_charge(run, lbl, reserved_usd, step.cost_usd)
            if self._scheduler:
                self._scheduler.release(lbl.org, lbl.team)
        except Exception as exc:
            step.finished_at = now_iso()
            step.latency_ms = (time.monotonic() - start_time) * 1000
            step.status = StepStatus.FAILED
            step.error = StepError(
                type=type(exc).__name__, message=str(exc)[:500],
                retryable=False, classified_reason="",
            )
            session.counters.steps_total += 1
            if self._controller:
                self._controller.feed_result(lbl.org, lbl.team, success=False)
            self._bus.emit(make_event(
                EventTypes.STEP_FAILED, session.run_id,
                session_id=session.session_id, step_id=step.step_id,
                parent_step_id=parent_step_id, labels=lbl,
                severity=Severity.ERROR,
                payload={
                    "kind": step.kind.value, "tool": tool_name,
                    "latency_ms": step.latency_ms,
                    "error_type": type(exc).__name__, "error_message": str(exc)[:500],
                },
            ))
            self._bus.emit(make_event(
                EventTypes.TOOL_CALL_FAILED, session.run_id,
                session_id=session.session_id, step_id=step.step_id,
                parent_step_id=parent_step_id, labels=lbl,
                severity=Severity.ERROR,
                payload={"tool": tool_name, "error_type": type(exc).__name__, "error_message": str(exc)[:500]},
            ))
            if self._enforcer and run:
                self._enforcer.post_charge(run, lbl, reserved_usd, step.cost_usd)
            if self._scheduler:
                self._scheduler.release(lbl.org, lbl.team)
            raise

    # --- Recording methods ---

    def record_retry(
        self, session: Session, step: Step,
        attempt: int, max_attempts: int,
        labels: Labels | None = None,
    ) -> None:
        lbl = labels or Labels()
        session.counters.retries_total += 1
        self._bus.emit(make_event(
            EventTypes.RETRY_SCHEDULED, session.run_id,
            session_id=session.session_id, step_id=step.step_id,
            labels=lbl,
            payload={"attempt": attempt, "max_attempts": max_attempts},
        ))
        if attempt >= max_attempts:
            self._bus.emit(make_event(
                EventTypes.RETRY_EXHAUSTED, session.run_id,
                session_id=session.session_id, step_id=step.step_id,
                labels=lbl, severity=Severity.WARN,
                payload={"attempt": attempt, "max_attempts": max_attempts},
            ))

    def record_breaker_change(
        self, run: Run, new_state: str, reason: str = "",
    ) -> None:
        type_map = {
            "open": EventTypes.BREAKER_OPENED,
            "half_open": EventTypes.BREAKER_HALF_OPEN,
            "closed": EventTypes.BREAKER_CLOSED,
        }
        event_type = type_map.get(new_state)
        if not event_type:
            return
        severity = Severity.WARN if new_state == "open" else Severity.INFO
        self._bus.emit(make_event(
            event_type, run.run_id,
            labels=run.labels, severity=severity,
            payload={"new_state": new_state, "reason": reason},
        ))
        if new_state == "open":
            old = run.status
            transition_run(run, RunStatus.DEGRADED, reason=reason)
            self._bus.emit(make_event(
                EventTypes.RUN_STATE_CHANGED, run.run_id,
                labels=run.labels,
                payload={"from": old.value, "to": RunStatus.DEGRADED.value, "reason": reason},
            ))

    def check_budget(self, run: Run) -> bool:
        self._bus.emit(make_event(
            EventTypes.BUDGET_CHECK, run.run_id,
            labels=run.labels,
            payload={
                "limit_usd": run.budget.limit_usd,
                "used_usd": run.budget.used_usd,
                "limit_tokens": run.budget.limit_tokens,
                "used_tokens": run.budget.used_tokens,
            },
        ))
        exceeded = (
            run.budget.limit_usd > 0 and run.budget.used_usd > run.budget.limit_usd
        ) or (
            run.budget.limit_tokens > 0 and run.budget.used_tokens > run.budget.limit_tokens
        )
        if exceeded:
            self._bus.emit(make_event(
                EventTypes.BUDGET_EXCEEDED, run.run_id,
                labels=run.labels, severity=Severity.CRITICAL,
                payload={
                    "limit_usd": run.budget.limit_usd,
                    "used_usd": run.budget.used_usd,
                    "limit_tokens": run.budget.limit_tokens,
                    "used_tokens": run.budget.used_tokens,
                },
            ))
            old = run.status
            transition_run(run, RunStatus.HALTED, reason="budget_exceeded")
            self._bus.emit(make_event(
                EventTypes.RUN_STATE_CHANGED, run.run_id,
                labels=run.labels,
                payload={"from": old.value, "to": RunStatus.HALTED.value, "reason": "budget_exceeded"},
            ))
        return exceeded

    def trigger_abort(self, run: Run, reason: str = "") -> None:
        self._bus.emit(make_event(
            EventTypes.ABORT_TRIGGERED, run.run_id,
            labels=run.labels, severity=Severity.CRITICAL,
            payload={"reason": reason},
        ))
        old = run.status
        transition_run(run, RunStatus.HALTED, reason=reason)
        self._bus.emit(make_event(
            EventTypes.RUN_STATE_CHANGED, run.run_id,
            labels=run.labels,
            payload={"from": old.value, "to": RunStatus.HALTED.value, "reason": reason},
        ))

    def trigger_timeout(self, session: Session, reason: str = "", labels: Labels | None = None) -> None:
        lbl = labels or Labels()
        self._bus.emit(make_event(
            EventTypes.TIMEOUT_TRIGGERED, session.run_id,
            session_id=session.session_id,
            labels=lbl, severity=Severity.WARN,
            payload={"reason": reason},
        ))
        transition_session(session, SessionStatus.HALTED, reason=reason)

    def record_loop_detected(self, session: Session, details: str = "", labels: Labels | None = None) -> None:
        if not session.loop_detection_on:
            return
        lbl = labels or Labels()
        self._bus.emit(make_event(
            EventTypes.LOOP_DETECTED, session.run_id,
            session_id=session.session_id,
            labels=lbl, severity=Severity.WARN,
            payload={"details": details},
        ))
        transition_session(session, SessionStatus.HALTED, reason="loop_detected")

    def preserve_partial(self, session: Session, data_ref: str = "", labels: Labels | None = None) -> None:
        lbl = labels or Labels()
        self._bus.emit(make_event(
            EventTypes.PARTIAL_PRESERVED, session.run_id,
            session_id=session.session_id,
            labels=lbl,
            payload={"data_ref": data_ref},
        ))

    def _check_max_steps(self, session: Session, labels: Labels) -> None:
        """Fail-fast if session has reached its max_steps limit.

        Semantics: max_steps > 0 enables enforcement. Allows exactly max_steps
        steps to complete. The (max_steps + 1)-th attempt to start a step raises
        MaxStepsExceeded. max_steps <= 0 means unlimited.
        """
        if session.max_steps <= 0:
            return
        if session.counters.steps_total >= session.max_steps:
            # Transition session to HALTED if still running
            if session.status == SessionStatus.RUNNING:
                transition_session(session, SessionStatus.HALTED, reason="max_steps_exceeded")
            self._bus.emit(make_event(
                EventTypes.MAX_STEPS_EXCEEDED,
                session.run_id,
                session_id=session.session_id,
                severity=Severity.WARN,
                labels=labels,
                payload={
                    "steps_executed": session.counters.steps_total,
                    "max_steps": session.max_steps,
                },
            ))
            raise MaxStepsExceeded(
                session.session_id,
                session.counters.steps_total,
                session.max_steps,
            )

    def _build_signals(self, labels: Labels) -> ControlSignals:
        """Build ControlSignals from current system state."""
        signals = ControlSignals()
        if self._controller:
            signals.consecutive_failures = self._controller.get_consecutive_failures(labels.org, labels.team)
        return signals
