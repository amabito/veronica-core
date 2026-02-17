"""VERONICA Runtime state machine — explicit transition maps for Run and Session."""
from __future__ import annotations

from veronica.runtime.models import (
    Run,
    RunStatus,
    Session,
    SessionStatus,
    now_iso,
)

# --- Transition maps ---

_RUN_TERMINAL = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED})

RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RUNNING: frozenset({
        RunStatus.DEGRADED, RunStatus.HALTED, RunStatus.QUARANTINED,
        RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED,
    }),
    RunStatus.DEGRADED: frozenset({
        RunStatus.RUNNING, RunStatus.HALTED, RunStatus.QUARANTINED,
        RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED,
    }),
    RunStatus.HALTED: frozenset({RunStatus.FAILED, RunStatus.CANCELED}),
    RunStatus.QUARANTINED: frozenset({RunStatus.HALTED, RunStatus.FAILED, RunStatus.CANCELED}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELED: frozenset(),
}

_SESSION_TERMINAL = frozenset({SessionStatus.SUCCEEDED, SessionStatus.FAILED, SessionStatus.CANCELED})

SESSION_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.RUNNING: frozenset({
        SessionStatus.HALTED, SessionStatus.SUCCEEDED,
        SessionStatus.FAILED, SessionStatus.CANCELED,
    }),
    SessionStatus.HALTED: frozenset({SessionStatus.FAILED, SessionStatus.CANCELED}),
    SessionStatus.SUCCEEDED: frozenset(),
    SessionStatus.FAILED: frozenset(),
    SessionStatus.CANCELED: frozenset(),
}


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, entity_type: str, from_status: str, to_status: str) -> None:
        self.entity_type = entity_type
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Invalid {entity_type} transition: {from_status} -> {to_status}"
        )


def is_terminal_run(status: RunStatus) -> bool:
    return status in _RUN_TERMINAL


def is_terminal_session(status: SessionStatus) -> bool:
    return status in _SESSION_TERMINAL


def transition_run(run: Run, new_status: RunStatus, reason: str = "") -> Run:
    """Transition a Run to a new status. Mutates in place. Raises InvalidTransitionError on invalid."""
    allowed = RUN_TRANSITIONS.get(run.status, frozenset())
    if new_status not in allowed:
        raise InvalidTransitionError("Run", run.status.value, new_status.value)
    run.status = new_status
    if is_terminal_run(new_status):
        run.finished_at = now_iso()
        if new_status == RunStatus.FAILED and reason:
            run.error_summary = reason
    return run


def transition_session(session: Session, new_status: SessionStatus, reason: str = "") -> Session:
    """Transition a Session to a new status. Mutates in place. Raises InvalidTransitionError on invalid."""
    allowed = SESSION_TRANSITIONS.get(session.status, frozenset())
    if new_status not in allowed:
        raise InvalidTransitionError("Session", session.status.value, new_status.value)
    session.status = new_status
    if is_terminal_session(new_status):
        session.finished_at = now_iso()
    return session
