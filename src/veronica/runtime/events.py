"""VERONICA Runtime event schema, types, and EventBus."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from veronica.runtime.models import Labels, Severity, generate_uuidv7, now_iso

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class EventTypes:
    """String constants for all event types."""

    # Run lifecycle
    RUN_CREATED = "run.created"
    RUN_STATE_CHANGED = "run.state_changed"
    RUN_FINISHED = "run.finished"

    # Session lifecycle
    SESSION_CREATED = "session.created"
    SESSION_FINISHED = "session.finished"

    # Step lifecycle
    STEP_STARTED = "step.started"
    STEP_SUCCEEDED = "step.succeeded"
    STEP_FAILED = "step.failed"

    # LLM calls
    LLM_CALL_STARTED = "llm.call.started"
    LLM_CALL_SUCCEEDED = "llm.call.succeeded"
    LLM_CALL_FAILED = "llm.call.failed"

    # Tool calls
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_SUCCEEDED = "tool.call.succeeded"
    TOOL_CALL_FAILED = "tool.call.failed"

    # Retry
    RETRY_SCHEDULED = "retry.scheduled"
    RETRY_EXHAUSTED = "retry.exhausted"

    # Circuit breaker
    BREAKER_OPENED = "breaker.opened"
    BREAKER_HALF_OPEN = "breaker.half_open"
    BREAKER_CLOSED = "breaker.closed"

    # Budget
    BUDGET_CHECK = "budget.check"
    BUDGET_EXCEEDED = "budget.exceeded"

    # Budget cgroup
    BUDGET_RESERVE_OK = "budget.reserve.ok"
    BUDGET_RESERVE_DENIED = "budget.reserve.denied"
    BUDGET_COMMIT = "budget.commit"
    BUDGET_THRESHOLD_CROSSED = "budget.threshold_crossed"

    # Control / Degrade
    CONTROL_LEVEL_CHANGED = "control.degrade.level_changed"
    CONTROL_DECISION_MADE = "control.decision.made"
    CONTROL_MODEL_DOWNGRADE = "control.action.model_downgrade"
    CONTROL_MAX_TOKENS_CAPPED = "control.action.max_tokens_capped"
    CONTROL_TOOLS_BLOCKED = "control.action.tools_blocked"
    CONTROL_SCHEDULER_MODE_CHANGED = "control.action.scheduler_mode_changed"

    # Control signals
    ABORT_TRIGGERED = "abort.triggered"
    TIMEOUT_TRIGGERED = "timeout.triggered"
    LOOP_DETECTED = "loop.detected"
    MAX_STEPS_EXCEEDED = "session.max_steps_exceeded"
    PARTIAL_PRESERVED = "partial.preserved"

    # Scheduler
    SCHEDULER_ADMIT_ALLOWED = "scheduler.admit.allowed"
    SCHEDULER_ADMIT_QUEUED = "scheduler.admit.queued"
    SCHEDULER_ADMIT_REJECTED = "scheduler.admit.rejected"
    SCHEDULER_QUEUE_ENQUEUED = "scheduler.queue.enqueued"
    SCHEDULER_QUEUE_DEQUEUED = "scheduler.queue.dequeued"
    SCHEDULER_QUEUE_DROPPED = "scheduler.queue.dropped"
    SCHEDULER_INFLIGHT_INC = "scheduler.inflight.inc"
    SCHEDULER_INFLIGHT_DEC = "scheduler.inflight.dec"
    SCHEDULER_PRIORITY_BOOST = "scheduler.priority_boost"


@dataclass
class Event:
    """Structured event for the VERONICA runtime."""

    event_id: str = field(default_factory=generate_uuidv7)
    ts: str = field(default_factory=now_iso)
    run_id: str = ""
    session_id: str | None = None
    step_id: str | None = None
    parent_step_id: str | None = None
    severity: Severity = Severity.INFO
    type: str = ""
    labels: Labels = field(default_factory=Labels)
    payload: dict[str, Any] = field(default_factory=dict)


def event_to_dict(event: Event) -> dict[str, Any]:
    """Convert an Event to a JSON-serializable dict."""
    d = asdict(event)
    # Severity enum -> string value
    d["severity"] = event.severity.value
    return d


def make_event(
    event_type: str,
    run_id: str,
    *,
    session_id: str | None = None,
    step_id: str | None = None,
    parent_step_id: str | None = None,
    severity: Severity = Severity.INFO,
    labels: Labels | None = None,
    payload: dict[str, Any] | None = None,
) -> Event:
    """Factory for creating events with auto-generated event_id and ts."""
    return Event(
        event_id=generate_uuidv7(),
        ts=now_iso(),
        run_id=run_id,
        session_id=session_id,
        step_id=step_id,
        parent_step_id=parent_step_id,
        severity=severity,
        type=event_type,
        labels=labels or Labels(),
        payload=payload or {},
    )


@runtime_checkable
class EventSinkProtocol(Protocol):
    """Protocol for event sinks."""

    def emit(self, event: Event) -> None: ...


class EventBus:
    """Dispatches events to all registered sinks."""

    def __init__(self, sinks: list[EventSinkProtocol] | None = None) -> None:
        self._sinks: list[EventSinkProtocol] = sinks or []

    def emit(self, event: Event) -> None:
        """Emit an event to all sinks. Individual sink errors are logged, not raised."""
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:
                logger.warning(
                    "EventBus: sink %s failed for event %s",
                    type(sink).__name__,
                    event.type,
                    exc_info=True,
                )

    def query_by_run_id(self, run_id: str) -> list[dict[str, Any]]:
        """Query events by run_id. Delegates to first sink that supports query."""
        for sink in self._sinks:
            if hasattr(sink, "query_by_run_id"):
                return sink.query_by_run_id(run_id)  # type: ignore[union-attr]
        return []
