"""VERONICA Scheduler queues — TeamQueue and WeightedFairQueue."""
from __future__ import annotations

import time
from collections import deque
from typing import Any

from veronica.scheduler.types import (
    Priority,
    QueueEntry,
    SchedulerConfig,
    priority_above,
    PRIORITY_ORDER,
)


class TeamQueue:
    """Per-team priority queue with 3 internal deques (P0, P1, P2)."""

    def __init__(self) -> None:
        self._queues: dict[Priority, deque[QueueEntry]] = {
            Priority.P0: deque(),
            Priority.P1: deque(),
            Priority.P2: deque(),
        }

    @property
    def size(self) -> int:
        return sum(len(q) for q in self._queues.values())

    @property
    def is_empty(self) -> bool:
        return self.size == 0

    def is_full(self, capacity: int) -> bool:
        return self.size >= capacity

    def enqueue(self, entry: QueueEntry, capacity: int) -> bool:
        """Append entry to priority deque. Returns False if at capacity."""
        if self.is_full(capacity):
            return False
        self._queues[entry.priority].append(entry)
        return True

    def dequeue(self) -> QueueEntry | None:
        """Pop from highest priority non-empty deque (P0 first)."""
        for priority in PRIORITY_ORDER:
            q = self._queues[priority]
            if q:
                return q.popleft()
        return None

    def peek_oldest(self) -> QueueEntry | None:
        """Peek at the oldest entry across all priorities (for tie-breaking)."""
        oldest: QueueEntry | None = None
        for q in self._queues.values():
            if q and (oldest is None or q[0].queued_at < oldest.queued_at):
                oldest = q[0]
        return oldest

    def promote_starved(self, threshold_ms: float, now: float | None = None) -> list[QueueEntry]:
        """Promote entries waiting longer than threshold to one priority level higher.
        Returns list of promoted entries."""
        if now is None:
            now = time.monotonic()
        threshold_s = threshold_ms / 1000.0
        promoted: list[QueueEntry] = []
        # Iterate P1 and P2 (P0 can't be promoted further)
        for priority in [Priority.P2, Priority.P1]:
            higher = priority_above(priority)
            if higher is None:
                continue
            q = self._queues[priority]
            remaining: deque[QueueEntry] = deque()
            while q:
                entry = q.popleft()
                waited = now - entry.queued_at
                if waited >= threshold_s:
                    entry.priority = higher
                    self._queues[higher].append(entry)
                    promoted.append(entry)
                else:
                    remaining.append(entry)
            self._queues[priority] = remaining
        return promoted


class WeightedFairQueue:
    """Org-level Weighted Round Robin queue across teams."""

    def __init__(self, config: SchedulerConfig) -> None:
        self._config = config
        self._team_queues: dict[str, TeamQueue] = {}
        self._deficits: dict[str, float] = {}

    def _ensure_team(self, team: str) -> TeamQueue:
        if team not in self._team_queues:
            self._team_queues[team] = TeamQueue()
            self._deficits[team] = 0.0
        return self._team_queues[team]

    def enqueue(self, entry: QueueEntry) -> bool:
        """Route entry to its team queue. Returns False if team queue is full."""
        tq = self._ensure_team(entry.team)
        ok = tq.enqueue(entry, self._config.team_queue_capacity)
        if ok:
            weight = self._config.get_team_weight(entry.team)
            self._deficits[entry.team] = self._deficits.get(entry.team, 0.0) + weight
        return ok

    def is_full(self, org_capacity: int) -> bool:
        """Check if total org queue is full."""
        return self.total_size >= org_capacity

    def dispatch(self) -> QueueEntry | None:
        """WRR dispatch: pick team with highest deficit, dequeue from it.
        Tie-breaking: team with oldest entry wins."""
        if self.total_size == 0:
            return None

        # Find non-empty teams sorted by deficit (desc), tie-break by oldest entry
        candidates: list[tuple[str, float, float]] = []
        for team_name, tq in self._team_queues.items():
            if not tq.is_empty:
                oldest = tq.peek_oldest()
                oldest_at = oldest.queued_at if oldest else float("inf")
                deficit = self._deficits.get(team_name, 0.0)
                candidates.append((team_name, deficit, oldest_at))

        if not candidates:
            return None

        # Sort: highest deficit first, then oldest entry first (tie-break)
        candidates.sort(key=lambda x: (-x[1], x[2]))
        chosen_team = candidates[0][0]

        entry = self._team_queues[chosen_team].dequeue()
        if entry is not None:
            self._deficits[chosen_team] = max(0.0, self._deficits[chosen_team] - 1.0)
        return entry

    @property
    def total_size(self) -> int:
        return sum(tq.size for tq in self._team_queues.values())

    def team_size(self, team: str) -> int:
        tq = self._team_queues.get(team)
        return tq.size if tq else 0

    def promote_all_starved(self, threshold_ms: float) -> list[QueueEntry]:
        """Promote starved entries across all team queues."""
        all_promoted: list[QueueEntry] = []
        now = time.monotonic()
        for tq in self._team_queues.values():
            promoted = tq.promote_starved(threshold_ms, now=now)
            all_promoted.extend(promoted)
        return all_promoted
