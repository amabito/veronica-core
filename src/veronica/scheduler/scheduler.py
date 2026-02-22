"""VERONICA Scheduler — admission control, dispatch, and concurrency gating."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from veronica.runtime.events import EventBus, EventTypes, make_event
from veronica.runtime.models import Labels, Severity
from veronica.scheduler.queue import WeightedFairQueue
from veronica.scheduler.types import (
    AdmitResult,
    QueueEntry,
    SchedulerConfig,
)


class Scheduler:
    """In-memory scheduler with admission control, WFQ dispatch, and concurrency gating."""

    def __init__(self, config: SchedulerConfig, bus: EventBus) -> None:
        self._config = config
        self._bus = bus
        self._queue = WeightedFairQueue(config)
        self._inflight_org: dict[str, int] = defaultdict(int)
        self._inflight_team: dict[tuple[str, str], int] = defaultdict(int)

    # --- Public API ---

    def admit(self, entry: QueueEntry) -> AdmitResult:
        """Check if a step can execute immediately, must queue, or is rejected.

        Returns:
            ALLOW: step can execute now (inflight counters incremented)
            QUEUE: step was queued (caller should retry or wait)
            REJECT: queue is full, step cannot proceed
        """
        org_count = self._inflight_org[entry.org]
        team_key = (entry.org, entry.team)
        team_count = self._inflight_team[team_key]

        can_run = (
            org_count < self._config.org_max_inflight
            and team_count < self._config.team_max_inflight
        )

        if can_run:
            self._acquire(entry)
            self._emit_admit_allowed(entry)
            return AdmitResult.ALLOW

        # Try to queue
        # Check org-level capacity first
        if self._queue.total_size >= self._config.org_queue_capacity:
            self._emit_admit_rejected(entry, "org_queue_full")
            return AdmitResult.REJECT

        ok = self._queue.enqueue(entry)
        if ok:
            reason = (
                "org_inflight_limit" if org_count >= self._config.org_max_inflight
                else "team_inflight_limit"
            )
            self._emit_admit_queued(entry, reason)
            self._emit_queue_enqueued(entry)
            return AdmitResult.QUEUE

        # Team queue full
        self._emit_admit_rejected(entry, "team_queue_full")
        return AdmitResult.REJECT

    def dispatch(self) -> QueueEntry | None:
        """Dequeue next entry via WFQ if inflight permits. Promotes starved entries first."""
        promoted = self._queue.promote_all_starved(self._config.starvation_threshold_ms)
        for p in promoted:
            self._emit_priority_boost(p)

        entry = self._queue.dispatch()
        if entry is None:
            return None

        self._acquire(entry)
        self._emit_queue_dequeued(entry)
        return entry

    def release(self, org: str, team: str) -> None:
        """Called when a step completes (succeeded/failed/canceled)."""
        self._inflight_org[org] = max(0, self._inflight_org[org] - 1)
        key = (org, team)
        self._inflight_team[key] = max(0, self._inflight_team[key] - 1)
        self._emit_inflight_dec(org, team)

    # --- Getters ---

    def org_inflight(self, org: str) -> int:
        return self._inflight_org[org]

    def team_inflight(self, org: str, team: str) -> int:
        return self._inflight_team[(org, team)]

    def org_queue_depth(self) -> int:
        return self._queue.total_size

    def team_queue_depth(self, team: str) -> int:
        return self._queue.team_size(team)

    # --- Internal ---

    def _acquire(self, entry: QueueEntry) -> None:
        self._inflight_org[entry.org] += 1
        self._inflight_team[(entry.org, entry.team)] += 1
        self._emit_inflight_inc(entry)

    def _make_labels(self, entry: QueueEntry) -> Labels:
        return Labels(org=entry.org, team=entry.team)

    def _inflight_snapshot(self, entry: QueueEntry) -> dict[str, Any]:
        return {
            "org_inflight": self._inflight_org[entry.org],
            "team_inflight": self._inflight_team[(entry.org, entry.team)],
        }

    def _queue_snapshot(self, entry: QueueEntry) -> dict[str, Any]:
        return {
            "org_queue_depth": self._queue.total_size,
            "team_queue_depth": self._queue.team_size(entry.team),
        }

    # --- Event emission ---

    def _emit_admit_allowed(self, entry: QueueEntry) -> None:
        self._bus.emit(make_event(
            EventTypes.SCHEDULER_ADMIT_ALLOWED,
            entry.run_id,
            session_id=entry.session_id,
            step_id=entry.step_id,
            labels=self._make_labels(entry),
            payload={
                "priority": entry.priority.value,
                "kind": entry.kind,
                **self._inflight_snapshot(entry),
            },
        ))

    def _emit_admit_queued(self, entry: QueueEntry, reason: str) -> None:
        self._bus.emit(make_event(
            EventTypes.SCHEDULER_ADMIT_QUEUED,
            entry.run_id,
            session_id=entry.session_id,
            step_id=entry.step_id,
            labels=self._make_labels(entry),
            severity=Severity.WARN,
            payload={
                "reason": reason,
                "priority": entry.priority.value,
                **self._inflight_snapshot(entry),
                **self._queue_snapshot(entry),
            },
        ))

    def _emit_admit_rejected(self, entry: QueueEntry, reason: str) -> None:
        self._bus.emit(make_event(
            EventTypes.SCHEDULER_ADMIT_REJECTED,
            entry.run_id,
            session_id=entry.session_id,
            step_id=entry.step_id,
            labels=self._make_labels(entry),
            severity=Severity.ERROR,
            payload={
                "reason": reason,
                "priority": entry.priority.value,
                **self._inflight_snapshot(entry),
                **self._queue_snapshot(entry),
            },
        ))

    def _emit_queue_enqueued(self, entry: QueueEntry) -> None:
        self._bus.emit(make_event(
            EventTypes.SCHEDULER_QUEUE_ENQUEUED,
            entry.run_id,
            session_id=entry.session_id,
            step_id=entry.step_id,
            labels=self._make_labels(entry),
            payload={
                "priority": entry.priority.value,
                **self._queue_snapshot(entry),
            },
        ))

    def _emit_queue_dequeued(self, entry: QueueEntry) -> None:
        self._bus.emit(make_event(
            EventTypes.SCHEDULER_QUEUE_DEQUEUED,
            entry.run_id,
            session_id=entry.session_id,
            step_id=entry.step_id,
            labels=self._make_labels(entry),
            payload={
                "priority": entry.priority.value,
                "waited_ms": ((time.monotonic() - entry.queued_at) * 1000),
                **self._queue_snapshot(entry),
            },
        ))

    def _emit_inflight_inc(self, entry: QueueEntry) -> None:
        self._bus.emit(make_event(
            EventTypes.SCHEDULER_INFLIGHT_INC,
            entry.run_id,
            session_id=entry.session_id,
            step_id=entry.step_id,
            labels=self._make_labels(entry),
            severity=Severity.DEBUG,
            payload=self._inflight_snapshot(entry),
        ))

    def _emit_inflight_dec(self, org: str, team: str) -> None:
        self._bus.emit(make_event(
            EventTypes.SCHEDULER_INFLIGHT_DEC,
            "",  # no run_id available at release time
            labels=Labels(org=org, team=team),
            severity=Severity.DEBUG,
            payload={
                "org_inflight": self._inflight_org[org],
                "team_inflight": self._inflight_team[(org, team)],
            },
        ))

    def _emit_priority_boost(self, entry: QueueEntry) -> None:
        self._bus.emit(make_event(
            EventTypes.SCHEDULER_PRIORITY_BOOST,
            entry.run_id,
            session_id=entry.session_id,
            step_id=entry.step_id,
            labels=self._make_labels(entry),
            severity=Severity.WARN,
            payload={
                "new_priority": entry.priority.value,
                "waited_ms": ((time.monotonic() - entry.queued_at) * 1000),
            },
        ))
