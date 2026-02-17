"""VERONICA demo scenarios — each returns a list[Event] collected via CollectorSink.

Four scenarios exercise the core VERONICA runtime sub-systems:
  S1 retry_cascade  — DegradeController escalation via consecutive LLM failures
  S2 budget_burn    — BudgetEnforcer threshold crossing and run halt
  S3 tool_hang      — DegradeController tool blocking and llm fallback
  S4 runaway_agent  — Scheduler admission control (queue/reject/dispatch)
"""
from __future__ import annotations

from veronica.runtime.hooks import RuntimeContext
from veronica.runtime.events import Event, EventBus
from veronica.runtime.models import Labels, Budget, Run, RunStatus
from veronica.budget.enforcer import BudgetEnforcer, BudgetExceeded
from veronica.budget.policy import BudgetPolicy, WindowLimit
from veronica.budget.ledger import BudgetLedger
from veronica.control.controller import DegradeController
from veronica.control.decision import DegradeConfig, DegradedToolBlocked, DegradedRejected
from veronica.scheduler.scheduler import Scheduler
from veronica.scheduler.types import SchedulerConfig, SchedulerQueued, SchedulerRejected
from veronica.demo.fakes import FakeProviderError, FakeToolTimeout


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class _CollectorSink:
    """Minimal EventSink that records all emitted events in memory."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


def _make_labels() -> Labels:
    return Labels(org="demo-corp", team="demo-team", user="demo-user")


def _finish_run(ctx: RuntimeContext, run: Run) -> None:
    """Finish a run, choosing FAILED when HALTED (state machine disallows SUCCEEDED from HALTED)."""
    if run.status == RunStatus.HALTED:
        ctx.finish_run(run, status=RunStatus.FAILED)
    elif run.status not in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED):
        ctx.finish_run(run, status=RunStatus.SUCCEEDED)
    # If already terminal, do nothing


# ---------------------------------------------------------------------------
# S1: retry_cascade
# ---------------------------------------------------------------------------

def retry_cascade() -> list[Event]:
    """Six LLM calls; first five raise FakeProviderError(429).

    Demonstrates:
    - llm.call.failed recorded on each failure
    - retry.scheduled / retry.exhausted after 3 retries
    - breaker.opened manually recorded after exhaustion
    - control.degrade.level_changed as consecutive_failures accumulate
    - Successful call 6 (recovery)
    """
    collector = _CollectorSink()
    bus = EventBus([collector])

    config = DegradeConfig(consecutive_fail_soft=2, consecutive_fail_hard=4)
    controller = DegradeController(config=config, bus=bus)
    ctx = RuntimeContext(sinks=[collector], controller=controller)

    lbl = _make_labels()
    run = ctx.create_run(labels=lbl)
    session = ctx.create_session(run, agent_name="retry-demo")

    max_attempts = 3
    failed_step = None

    for attempt in range(1, 7):
        should_fail = attempt <= 5
        try:
            with ctx.llm_call(session, model="gpt-4o", provider="openai", labels=lbl, run=run) as step:
                if should_fail:
                    raise FakeProviderError(status_code=429, message="rate limited")
                # Call 6 succeeds
                step.tokens_in = 100
                step.tokens_out = 50
                step.cost_usd = 0.005
        except FakeProviderError:
            ctx.record_retry(session, step, attempt=attempt, max_attempts=max_attempts, labels=lbl)
            failed_step = step
            if attempt == max_attempts:
                ctx.record_breaker_change(run, "open", reason="429_storm")

    ctx.finish_session(session, labels=lbl)
    _finish_run(ctx, run)

    return collector.events


# ---------------------------------------------------------------------------
# S2: budget_burn
# ---------------------------------------------------------------------------

def budget_burn() -> list[Event]:
    """LLM calls consume budget until BudgetExceeded is raised.

    Demonstrates:
    - budget.reserve.ok on early calls
    - budget.commit after each successful call
    - budget.threshold_crossed at 80%, 90%, 100%
    - run.state_changed (RUNNING -> DEGRADED -> HALTED)
    - budget.reserve.denied when minute limit is exhausted
    """
    collector = _CollectorSink()
    bus = EventBus([collector])

    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=0.05, hour_usd=0.50, day_usd=5.0),
        thresholds=[0.8, 0.9, 1.0],
    )
    ledger = BudgetLedger()
    enforcer = BudgetEnforcer(policy=policy, ledger=ledger, bus=bus)

    ctx = RuntimeContext(sinks=[collector], enforcer=enforcer)

    lbl = _make_labels()
    run = ctx.create_run(labels=lbl, budget=Budget(limit_usd=0.10))
    session = ctx.create_session(run, agent_name="budget-demo")

    for i in range(10):
        try:
            with ctx.llm_call(session, model="gpt-4o", provider="openai", labels=lbl, run=run) as step:
                # Each successful call costs $0.01
                step.tokens_in = 200
                step.tokens_out = 100
                step.cost_usd = 0.01
        except BudgetExceeded:
            # Budget exhausted — stop gracefully
            break

    ctx.finish_session(session, labels=lbl)
    _finish_run(ctx, run)

    return collector.events


# ---------------------------------------------------------------------------
# S3: tool_hang
# ---------------------------------------------------------------------------

def tool_hang() -> list[Event]:
    """Tool calls time out until the controller blocks tools; fallback to llm_call.

    Demonstrates:
    - tool.call.failed x3 (FakeToolTimeout)
    - control.degrade.level_changed NORMAL->SOFT->HARD via consecutive failures
    - DegradedToolBlocked raised and caught on call 4
    - Successful llm_call as graceful fallback on call 5
    """
    collector = _CollectorSink()
    bus = EventBus([collector])

    config = DegradeConfig(consecutive_fail_soft=2, consecutive_fail_hard=3)
    controller = DegradeController(config=config, bus=bus)
    ctx = RuntimeContext(sinks=[collector], controller=controller)

    lbl = _make_labels()
    run = ctx.create_run(labels=lbl)
    session = ctx.create_session(run, agent_name="tool-hang-demo")

    # Calls 1-3: tool failures escalate degrade level.
    # Use read_only_tools=frozenset({"web_search"}) so SOFT-level does not block read-only tools.
    # At HARD level (consecutive_fail_hard=3) all tools are blocked regardless.
    read_only = frozenset({"web_search"})
    for _ in range(3):
        try:
            with ctx.tool_call(
                session, tool_name="web_search", labels=lbl, run=run,
                read_only_tools=read_only,
            ) as step:
                raise FakeToolTimeout()
        except FakeToolTimeout:
            pass
        except DegradedToolBlocked:
            # HARD level reached mid-loop — record and stop iterating
            break

    # Call 4: controller should block at HARD level (DegradedToolBlocked raised before yield)
    try:
        with ctx.tool_call(
            session, tool_name="web_search", labels=lbl, run=run,
            read_only_tools=read_only,
        ) as step:
            pass  # should not reach here
    except DegradedToolBlocked:
        pass

    # Call 5: fall back to llm_call — succeeds
    try:
        with ctx.llm_call(session, model="gpt-4o", provider="openai", labels=lbl, run=run, priority="P0") as step:
            step.tokens_in = 50
            step.tokens_out = 25
            step.cost_usd = 0.003
    except (DegradedRejected, Exception):
        pass

    ctx.finish_session(session, labels=lbl)
    _finish_run(ctx, run)

    return collector.events


# ---------------------------------------------------------------------------
# S4: runaway_agent
# ---------------------------------------------------------------------------

def runaway_agent() -> list[Event]:
    """Rapid calls hit scheduler limits; queued entry dispatched after release.

    Demonstrates:
    - scheduler.admit.allowed (call 1 runs immediately)
    - scheduler.admit.queued / SchedulerQueued raised (call 2)
    - scheduler.admit.rejected / SchedulerRejected raised (call 3)
    - Release inflight slot and dispatch queued entry
    - Final call succeeds normally
    """
    collector = _CollectorSink()
    bus = EventBus([collector])

    sched_config = SchedulerConfig(
        org_max_inflight=2,
        team_max_inflight=1,
        org_queue_capacity=2,
        team_queue_capacity=1,
    )
    scheduler = Scheduler(config=sched_config, bus=bus)

    ctx = RuntimeContext(sinks=[collector], scheduler=scheduler)

    lbl = _make_labels()
    run = ctx.create_run(labels=lbl)
    session = ctx.create_session(run, agent_name="runaway-demo")

    # Call 1: admitted immediately (team_inflight 0 -> 1)
    step1 = None
    with ctx.llm_call(session, model="gpt-4o", provider="openai", labels=lbl, run=run) as step:
        step.tokens_in = 10
        step.tokens_out = 5
        step.cost_usd = 0.001
        step1 = step
    # step1 succeeded; inflight released automatically in hooks.py

    # Fill inflight to saturation so call 2 queues
    # Re-acquire inflight slot manually to simulate concurrency
    from veronica.scheduler.types import QueueEntry, Priority
    from veronica.runtime.models import generate_uuidv7
    blocker = QueueEntry(
        step_id=generate_uuidv7(),
        run_id=run.run_id,
        session_id=session.session_id,
        org=lbl.org,
        team=lbl.team,
        priority=Priority.P1,
        kind="llm_call",
        model="gpt-4o",
    )
    # Force a slot to be occupied so next call must queue
    scheduler._acquire(blocker)  # type: ignore[attr-defined]

    # Call 2: team_inflight == 1 == team_max_inflight -> queued
    try:
        with ctx.llm_call(session, model="gpt-4o", provider="openai", labels=lbl, run=run) as step:
            pass
    except SchedulerQueued:
        pass

    # Call 3: org capacity still has room but team queue now full (capacity=1, already 1 queued)
    # Fill the team queue first to ensure rejection
    blocker2 = QueueEntry(
        step_id=generate_uuidv7(),
        run_id=run.run_id,
        session_id=session.session_id,
        org=lbl.org,
        team=lbl.team,
        priority=Priority.P1,
        kind="llm_call",
        model="gpt-4o",
    )
    scheduler._queue.enqueue(blocker2)  # type: ignore[attr-defined]

    try:
        with ctx.llm_call(session, model="gpt-4o", provider="openai", labels=lbl, run=run) as step:
            pass
    except SchedulerRejected:
        pass

    # Release the manually-acquired blocker slot, then dispatch queued entry
    scheduler.release(lbl.org, lbl.team)
    dispatched = scheduler.dispatch()
    # Release the dispatched entry too (demo only — simulates it completing instantly)
    if dispatched is not None:
        scheduler.release(lbl.org, lbl.team)

    # Final call: inflight slot now available -> ALLOW
    with ctx.llm_call(session, model="gpt-4o", provider="openai", labels=lbl, run=run) as step:
        step.tokens_in = 20
        step.tokens_out = 10
        step.cost_usd = 0.002

    ctx.finish_session(session, labels=lbl)
    _finish_run(ctx, run)

    return collector.events
