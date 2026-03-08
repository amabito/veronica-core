"""Tests for _ChainEventLog -- Phase 2 ExecutionContext decomposition.

Covers:
- append: basic event storage
- dedup: same event not stored twice
- cap: _MAX_CHAIN_EVENTS prevents unbounded growth
- emit_chain_event: generates SafetyEvent with correct type mapping
- Thread safety: 10 threads appending events simultaneously
- snapshot: returns copy, internal state unchanged
"""

from __future__ import annotations

import threading

import pytest

from veronica_core.containment._chain_event_log import (
    _ChainEventLog,
    _MAX_CHAIN_EVENTS,
)
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_type: str = "TEST_EVENT",
    decision: Decision = Decision.HALT,
    reason: str = "test reason",
    hook: str = "TestHook",
    request_id: str = "req-1",
) -> SafetyEvent:
    return SafetyEvent(
        event_type=event_type,
        decision=decision,
        reason=reason,
        hook=hook,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


class TestAppend:
    """Basic event storage via append()."""

    def test_empty_log_has_zero_length(self) -> None:
        log = _ChainEventLog()
        assert len(log) == 0

    def test_append_stores_event(self) -> None:
        log = _ChainEventLog()
        ev = _make_event()
        log.append(ev)
        assert len(log) == 1

    def test_snapshot_contains_appended_event(self) -> None:
        log = _ChainEventLog()
        ev = _make_event(event_type="MY_EVENT")
        log.append(ev)
        snap = log.snapshot()
        assert snap[0].event_type == "MY_EVENT"

    def test_multiple_events_stored_in_order(self) -> None:
        log = _ChainEventLog()
        for i in range(5):
            log.append(_make_event(event_type=f"EVENT_{i}", request_id=f"req-{i}"))
        snap = log.snapshot()
        assert [e.event_type for e in snap] == [f"EVENT_{i}" for i in range(5)]

    def test_append_batch_stores_all(self) -> None:
        log = _ChainEventLog()
        events = [
            _make_event(event_type=f"BATCH_{i}", request_id=f"r{i}") for i in range(3)
        ]
        log.append_batch(events)
        assert len(log) == 3


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDedup:
    """Same event (by 5-tuple key) must not be stored twice."""

    def test_identical_event_appended_twice_stored_once(self) -> None:
        log = _ChainEventLog()
        ev = _make_event()
        log.append(ev)
        log.append(ev)
        assert len(log) == 1

    def test_events_differing_by_type_both_stored(self) -> None:
        log = _ChainEventLog()
        log.append(_make_event(event_type="TYPE_A"))
        log.append(_make_event(event_type="TYPE_B"))
        assert len(log) == 2

    def test_events_differing_by_reason_both_stored(self) -> None:
        log = _ChainEventLog()
        log.append(_make_event(reason="reason_a"))
        log.append(_make_event(reason="reason_b"))
        assert len(log) == 2

    def test_events_differing_by_request_id_both_stored(self) -> None:
        log = _ChainEventLog()
        log.append(_make_event(request_id="req-a"))
        log.append(_make_event(request_id="req-b"))
        assert len(log) == 2

    def test_events_differing_by_hook_both_stored(self) -> None:
        log = _ChainEventLog()
        log.append(_make_event(hook="HookA"))
        log.append(_make_event(hook="HookB"))
        assert len(log) == 2

    def test_dedup_across_batch_and_single_append(self) -> None:
        log = _ChainEventLog()
        ev = _make_event(event_type="X")
        log.append(ev)
        log.append_batch([ev])  # duplicate
        assert len(log) == 1


# ---------------------------------------------------------------------------
# Cap
# ---------------------------------------------------------------------------


class TestCap:
    """_MAX_CHAIN_EVENTS prevents unbounded growth."""

    def test_cap_constant_is_positive(self) -> None:
        assert _MAX_CHAIN_EVENTS > 0

    def test_events_beyond_cap_silently_dropped(self) -> None:
        log = _ChainEventLog()
        # Insert _MAX_CHAIN_EVENTS + 10 unique events
        for i in range(_MAX_CHAIN_EVENTS + 10):
            log.append(_make_event(event_type=f"E_{i}", request_id=str(i)))
        assert len(log) == _MAX_CHAIN_EVENTS

    def test_cap_reached_exactly_at_max(self) -> None:
        log = _ChainEventLog()
        for i in range(_MAX_CHAIN_EVENTS):
            log.append(_make_event(event_type=f"E_{i}", request_id=str(i)))
        assert len(log) == _MAX_CHAIN_EVENTS

    def test_one_event_past_cap_does_not_raise(self) -> None:
        log = _ChainEventLog()
        for i in range(_MAX_CHAIN_EVENTS + 1):
            log.append(_make_event(event_type=f"E_{i}", request_id=str(i)))
        # No exception raised, length capped
        assert len(log) == _MAX_CHAIN_EVENTS


# ---------------------------------------------------------------------------
# emit_chain_event
# ---------------------------------------------------------------------------


class TestEmitChainEvent:
    """emit_chain_event generates SafetyEvent with correct type mapping."""

    @pytest.mark.parametrize(
        "stop_reason,expected_event_type",
        [
            ("aborted", "CHAIN_ABORTED"),
            ("budget_exceeded", "CHAIN_BUDGET_EXCEEDED"),
            ("budget_exceeded_by_child", "CHAIN_BUDGET_EXCEEDED_BY_CHILD"),
            ("step_limit_exceeded", "CHAIN_STEP_LIMIT_EXCEEDED"),
            ("retry_budget_exceeded", "CHAIN_RETRY_BUDGET_EXCEEDED"),
            ("timeout", "CHAIN_TIMEOUT"),
            ("circuit_open", "CHAIN_CIRCUIT_OPEN"),
        ],
    )
    def test_known_stop_reason_maps_to_correct_event_type(
        self, stop_reason: str, expected_event_type: str
    ) -> None:
        log = _ChainEventLog()
        log.emit_chain_event(stop_reason, "some detail", "req-123")
        snap = log.snapshot()
        assert len(snap) == 1
        assert snap[0].event_type == expected_event_type

    def test_unknown_stop_reason_falls_back_to_uppercase(self) -> None:
        log = _ChainEventLog()
        log.emit_chain_event("custom_reason", "detail", "req-1")
        snap = log.snapshot()
        assert snap[0].event_type == "CUSTOM_REASON"

    def test_emitted_event_has_halt_decision(self) -> None:
        log = _ChainEventLog()
        log.emit_chain_event("timeout", "expired", "req-1")
        snap = log.snapshot()
        assert snap[0].decision == Decision.HALT

    def test_emitted_event_has_execution_context_hook(self) -> None:
        log = _ChainEventLog()
        log.emit_chain_event("aborted", "manual stop", "req-1")
        snap = log.snapshot()
        assert snap[0].hook == "ExecutionContext"

    def test_emitted_event_preserves_detail_as_reason(self) -> None:
        log = _ChainEventLog()
        detail = "steps 50 >= limit 50"
        log.emit_chain_event("step_limit_exceeded", detail, "req-1")
        snap = log.snapshot()
        assert snap[0].reason == detail

    def test_emitted_event_preserves_request_id(self) -> None:
        log = _ChainEventLog()
        log.emit_chain_event("aborted", "test", "my-request-id")
        snap = log.snapshot()
        assert snap[0].request_id == "my-request-id"

    def test_duplicate_emit_stored_once(self) -> None:
        log = _ChainEventLog()
        log.emit_chain_event("timeout", "expired", "req-1")
        log.emit_chain_event("timeout", "expired", "req-1")
        assert len(log) == 1

    def test_same_reason_different_request_id_stored_separately(self) -> None:
        log = _ChainEventLog()
        log.emit_chain_event("aborted", "reason", "req-A")
        log.emit_chain_event("aborted", "reason", "req-B")
        assert len(log) == 2


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    """snapshot() returns a shallow copy; internal list is unchanged."""

    def test_snapshot_is_a_copy(self) -> None:
        log = _ChainEventLog()
        log.append(_make_event())
        snap = log.snapshot()
        snap.clear()
        # Original log must be unaffected
        assert len(log) == 1

    def test_snapshot_returns_list(self) -> None:
        log = _ChainEventLog()
        assert isinstance(log.snapshot(), list)

    def test_snapshot_empty_when_no_events(self) -> None:
        log = _ChainEventLog()
        assert log.snapshot() == []

    def test_multiple_snapshots_return_identical_content(self) -> None:
        log = _ChainEventLog()
        log.append(_make_event(event_type="E1", request_id="r1"))
        snap1 = log.snapshot()
        snap2 = log.snapshot()
        assert [e.event_type for e in snap1] == [e.event_type for e in snap2]


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """10 threads appending events simultaneously -- no data corruption."""

    def test_concurrent_append_all_unique_events(self) -> None:
        log = _ChainEventLog()
        n_threads = 10
        events_per_thread = 50
        # Each thread appends unique events so no dedup occurs
        barrier = threading.Barrier(n_threads)

        def worker(thread_id: int) -> None:
            barrier.wait()
            for i in range(events_per_thread):
                ev = _make_event(
                    event_type=f"T{thread_id}_E{i}",
                    request_id=f"t{thread_id}-{i}",
                )
                log.append(ev)

        threads = [
            threading.Thread(target=worker, args=(tid,)) for tid in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = n_threads * events_per_thread
        assert len(log) == min(total, _MAX_CHAIN_EVENTS)

    def test_concurrent_emit_chain_event_no_crash(self) -> None:
        log = _ChainEventLog()
        barrier = threading.Barrier(10)
        errors: list[Exception] = []

        def worker(tid: int) -> None:
            try:
                barrier.wait()
                for i in range(20):
                    log.emit_chain_event("timeout", f"detail-{tid}-{i}", f"req-{tid}-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent emit raised: {errors[0]}"

    def test_concurrent_append_and_snapshot_no_corruption(self) -> None:
        """Snapshot while appending must not raise or return corrupted data."""
        log = _ChainEventLog()
        stop = threading.Event()
        errors: list[Exception] = []

        def appender() -> None:
            i = 0
            while not stop.is_set():
                try:
                    log.append(_make_event(event_type=f"E{i}", request_id=str(i)))
                    i += 1
                except Exception as exc:
                    errors.append(exc)
                    return

        def reader() -> None:
            for _ in range(200):
                try:
                    snap = log.snapshot()
                    assert isinstance(snap, list)
                except Exception as exc:
                    errors.append(exc)
                    return

        writer_threads = [threading.Thread(target=appender) for _ in range(3)]
        reader_threads = [threading.Thread(target=reader) for _ in range(3)]
        for t in writer_threads + reader_threads:
            t.start()
        for t in reader_threads:
            t.join()
        stop.set()
        for t in writer_threads:
            t.join()

        assert errors == [], f"Concurrent read/write raised: {errors[0]}"
