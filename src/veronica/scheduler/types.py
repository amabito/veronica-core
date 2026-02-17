"""VERONICA Scheduler types — enums, exceptions, config, queue entry."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Priority(str, Enum):
    """Scheduling priority levels."""
    P0 = "P0"  # user-facing / prod
    P1 = "P1"  # normal (default)
    P2 = "P2"  # batch / offline


# Priority ordering for comparison (lower index = higher priority)
PRIORITY_ORDER: list[Priority] = [Priority.P0, Priority.P1, Priority.P2]


def priority_above(p: Priority) -> Priority | None:
    """Return the priority one level above, or None if already P0."""
    idx = PRIORITY_ORDER.index(p)
    if idx == 0:
        return None
    return PRIORITY_ORDER[idx - 1]


class AdmitResult(str, Enum):
    """Result of scheduler admission check."""
    ALLOW = "allow"
    QUEUE = "queue"
    REJECT = "reject"


class SchedulerQueued(Exception):
    """Step was queued, not immediately executed. Caller should retry later."""
    def __init__(self, step_id: str = "", reason: str = "") -> None:
        self.step_id = step_id
        self.reason = reason
        super().__init__(f"Step {step_id} queued: {reason}")


class SchedulerRejected(Exception):
    """Step was rejected by the scheduler."""
    def __init__(self, step_id: str = "", reason: str = "") -> None:
        self.step_id = step_id
        self.reason = reason
        super().__init__(f"Step {step_id} rejected: {reason}")


class SchedulerQueueFull(SchedulerRejected):
    """Queue capacity exceeded — step cannot be queued."""
    pass


@dataclass
class SchedulerConfig:
    """Configuration for the scheduler."""
    org_max_inflight: int = 32
    team_max_inflight: int = 8
    org_queue_capacity: int = 10_000
    team_queue_capacity: int = 2_000
    starvation_threshold_ms: float = 30_000.0  # 30 seconds
    team_weights: dict[str, int] = field(default_factory=dict)  # team_name -> weight

    def get_team_weight(self, team: str) -> int:
        """Get weight for a team (default 1)."""
        return self.team_weights.get(team, 1)


@dataclass
class QueueEntry:
    """An entry in the scheduler queue."""
    step_id: str
    run_id: str
    session_id: str
    org: str
    team: str
    priority: Priority = Priority.P1
    queued_at: float = field(default_factory=time.monotonic)
    kind: str = "llm_call"
    model: str = ""
