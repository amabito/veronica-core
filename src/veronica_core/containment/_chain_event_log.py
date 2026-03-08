"""Internal chain-event-log helper for ExecutionContext.

_ChainEventLog owns the SafetyEvent list and dedup key set, and exposes
thread-safe append / emit / drain operations.

This module is package-internal (_-prefix); do NOT import it from outside
veronica_core.containment.
"""

from __future__ import annotations

import threading
from typing import Any

from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision

# Maximum number of SafetyEvents stored per chain to prevent memory
# exhaustion from flooding callers or repeated limit-check emissions.
_MAX_CHAIN_EVENTS: int = 1_000

_STOP_REASON_EVENT_TYPE: dict[str, str] = {
    "aborted": "CHAIN_ABORTED",
    "budget_exceeded": "CHAIN_BUDGET_EXCEEDED",
    "budget_exceeded_by_child": "CHAIN_BUDGET_EXCEEDED_BY_CHILD",
    "step_limit_exceeded": "CHAIN_STEP_LIMIT_EXCEEDED",
    "retry_budget_exceeded": "CHAIN_RETRY_BUDGET_EXCEEDED",
    "timeout": "CHAIN_TIMEOUT",
    "circuit_open": "CHAIN_CIRCUIT_OPEN",
    "memory_governance_denied": "CHAIN_MEMORY_GOVERNANCE_DENIED",
}


class _ChainEventLog:
    """Thread-safe append-only log of SafetyEvents for a single chain.

    Caps storage at _MAX_CHAIN_EVENTS and deduplicates by a 5-tuple content
    key (event_type, decision, reason, hook, request_id) to prevent duplicate
    limit-check emissions.

    The caller (ExecutionContext) may hold its own outer lock when calling
    methods here; _ChainEventLog has its own internal lock for its state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[SafetyEvent] = []
        self._dedup_keys: set[tuple[str, Decision, str, str, str | None]] = set()

    # ------------------------------------------------------------------
    # Append helpers
    # ------------------------------------------------------------------

    def append(self, event: SafetyEvent) -> None:
        """Append *event* to the log, deduplicating by content fields.

        Silently drops the event when the cap is reached or the event is
        a duplicate (same event_type, decision, reason, hook, request_id).
        """
        with self._lock:
            self._append_locked(event)

    def append_batch(self, events: list[SafetyEvent]) -> None:
        """Append multiple events under a single lock acquisition."""
        with self._lock:
            for ev in events:
                self._append_locked(ev)

    def _append_locked(self, event: SafetyEvent) -> None:
        """Append *event* without acquiring the lock (caller must hold it)."""
        dk = (
            event.event_type,
            event.decision,
            event.reason,
            event.hook,
            event.request_id,
        )
        if len(self._events) < _MAX_CHAIN_EVENTS and dk not in self._dedup_keys:
            self._events.append(event)
            self._dedup_keys.add(dk)

    def emit_chain_event(
        self,
        stop_reason: str,
        detail: str,
        request_id: str,
        policy_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Build and append a chain-level SafetyEvent for *stop_reason*.

        Thread-safe.

        Args:
            stop_reason: Key from _STOP_REASON_EVENT_TYPE (or raw str as fallback).
            detail: Human-readable explanation.
            request_id: The chain's request_id for the event.
            policy_metadata: Optional policy audit dict from FrozenPolicyView.
                If provided, stored in the event's metadata under the
                ``"policy"`` key for audit trail enrichment (v3.3).
        """
        event_type = _STOP_REASON_EVENT_TYPE.get(stop_reason, stop_reason.upper())
        metadata: dict[str, Any] = {}
        if policy_metadata is not None:
            metadata["policy"] = policy_metadata
        event = SafetyEvent(
            event_type=event_type,
            decision=Decision.HALT,
            reason=detail,
            hook="ExecutionContext",
            request_id=request_id,
            metadata=metadata,
        )
        with self._lock:
            self._append_locked(event)

    # ------------------------------------------------------------------
    # Snapshot / drain
    # ------------------------------------------------------------------

    def snapshot(self) -> list[SafetyEvent]:
        """Return a shallow copy of the event list under the lock."""
        with self._lock:
            return list(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)
