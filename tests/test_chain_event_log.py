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
                    log.emit_chain_event(
                        "timeout", f"detail-{tid}-{i}", f"req-{tid}-{i}"
                    )
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


# ---------------------------------------------------------------------------
# Dedup dimension coverage (v3.2.0)
# ---------------------------------------------------------------------------


class TestDedupDimensions:
    """Verify dedup distinguishes all 5 key dimensions independently."""

    def test_same_event_different_decision_stored_separately(self) -> None:
        log = _ChainEventLog()
        e1 = SafetyEvent(
            event_type="T",
            decision=Decision.HALT,
            reason="r",
            hook="h",
            request_id="req",
        )
        e2 = SafetyEvent(
            event_type="T",
            decision=Decision.ALLOW,
            reason="r",
            hook="h",
            request_id="req",
        )
        log.append(e1)
        log.append(e2)
        assert len(log) == 2

    def test_same_event_different_hook_stored_separately(self) -> None:
        log = _ChainEventLog()
        e1 = SafetyEvent(
            event_type="T",
            decision=Decision.HALT,
            reason="r",
            hook="hook_a",
            request_id="req",
        )
        e2 = SafetyEvent(
            event_type="T",
            decision=Decision.HALT,
            reason="r",
            hook="hook_b",
            request_id="req",
        )
        log.append(e1)
        log.append(e2)
        assert len(log) == 2

    def test_none_request_id_deduped_correctly(self) -> None:
        log = _ChainEventLog()
        e1 = SafetyEvent(
            event_type="T",
            decision=Decision.HALT,
            reason="r",
            hook="h",
            request_id=None,
        )
        e2 = SafetyEvent(
            event_type="T",
            decision=Decision.HALT,
            reason="r",
            hook="h",
            request_id=None,
        )
        log.append(e1)
        log.append(e2)
        assert len(log) == 1  # deduped


# ---------------------------------------------------------------------------
# Adversarial: Flooding / Resource Exhaustion
# ---------------------------------------------------------------------------


class TestAdversarialChainEventLogFlooding:
    """Attacker mindset: flood the log with events to exhaust memory or bypass cap."""

    def test_cap_plus_one_unique_events_hard_cap_enforced(self) -> None:
        """Append _MAX_CHAIN_EVENTS+1 unique events -- cap must hold exactly."""
        log = _ChainEventLog()
        for i in range(_MAX_CHAIN_EVENTS + 1):
            log.append(_make_event(event_type=f"FLOOD_{i}", request_id=str(i)))
        # Cap is a hard ceiling -- never exceed
        assert len(log) == _MAX_CHAIN_EVENTS
        # Dedup key set must not grow beyond cap either
        snap = log.snapshot()
        assert len(snap) == _MAX_CHAIN_EVENTS

    def test_ten_thousand_duplicate_events_stored_once(self) -> None:
        """10,000 identical events -- dedup prevents storage bloat."""
        log = _ChainEventLog()
        ev = _make_event(event_type="DUP", request_id="dup-req")
        for _ in range(10_000):
            log.append(ev)
        # Only 1 stored despite 10,000 calls
        assert len(log) == 1
        snap = log.snapshot()
        assert len(snap) == 1

    def test_emit_chain_event_with_one_mb_reason_no_crash(self) -> None:
        """emit_chain_event with a 1 MB reason string must not raise."""
        log = _ChainEventLog()
        big_reason = "x" * (1024 * 1024)  # 1 MB
        log.emit_chain_event("timeout", big_reason, "req-big")
        snap = log.snapshot()
        assert len(snap) == 1
        # Stored reason must be exactly the big string (no silent truncation)
        assert snap[0].reason == big_reason

    def test_append_batch_5000_events_cap_enforced(self) -> None:
        """append_batch with 5,000 unique events -- cap still enforced after batch."""
        log = _ChainEventLog()
        batch = [
            _make_event(event_type=f"BATCH_{i}", request_id=str(i))
            for i in range(5_000)
        ]
        log.append_batch(batch)
        # Must never exceed _MAX_CHAIN_EVENTS
        assert len(log) == _MAX_CHAIN_EVENTS
        snap = log.snapshot()
        assert len(snap) == _MAX_CHAIN_EVENTS

    def test_append_batch_mixed_duplicates_and_uniques_cap_respected(self) -> None:
        """Batch containing both duplicates and uniques -- cap is the ceiling."""
        log = _ChainEventLog()
        # Fill log to cap first
        for i in range(_MAX_CHAIN_EVENTS):
            log.append(_make_event(event_type=f"PRE_{i}", request_id=str(i)))
        assert len(log) == _MAX_CHAIN_EVENTS
        # Now batch-append 100 more (all new uniques) -- must all be dropped
        extra = [
            _make_event(event_type=f"EXTRA_{i}", request_id=f"extra-{i}")
            for i in range(100)
        ]
        log.append_batch(extra)
        assert len(log) == _MAX_CHAIN_EVENTS


# ---------------------------------------------------------------------------
# Adversarial: TOCTOU / Race Conditions
# ---------------------------------------------------------------------------


class TestAdversarialChainEventLogRace:
    """Attacker mindset: concurrent access must preserve cap, dedup, and snapshot integrity."""

    def test_10_threads_100_unique_events_each_total_at_most_cap(self) -> None:
        """10 threads x 100 unique events = 1,000 total -- cap never exceeded."""
        log = _ChainEventLog()
        n_threads = 10
        events_per_thread = 100
        barrier = threading.Barrier(n_threads)

        def worker(tid: int) -> None:
            barrier.wait()
            for i in range(events_per_thread):
                log.append(
                    _make_event(
                        event_type=f"T{tid}_E{i}",
                        request_id=f"t{tid}-{i}",
                    )
                )

        threads = [
            threading.Thread(target=worker, args=(tid,)) for tid in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total_attempted = n_threads * events_per_thread
        assert len(log) <= _MAX_CHAIN_EVENTS
        # When total_attempted <= cap, all must be stored (no dedup here)
        assert len(log) == min(total_attempted, _MAX_CHAIN_EVENTS)

    def test_5_writer_5_snapshot_threads_no_partial_state(self) -> None:
        """5 writers + 5 readers -- snapshot must always return a consistent list."""
        log = _ChainEventLog()
        errors: list[Exception] = []
        stop_flag = threading.Event()
        barrier = threading.Barrier(10)

        def writer(tid: int) -> None:
            barrier.wait()
            for i in range(200):
                if stop_flag.is_set():
                    break
                try:
                    log.append(
                        _make_event(
                            event_type=f"W{tid}_{i}",
                            request_id=f"w{tid}-{i}",
                        )
                    )
                except Exception as exc:
                    errors.append(exc)
                    return

        def reader() -> None:
            barrier.wait()
            for _ in range(100):
                try:
                    snap = log.snapshot()
                    # Snapshot must always be a proper list -- no RuntimeError from mutation
                    assert isinstance(snap, list)
                    for ev in snap:
                        # Each element must be a SafetyEvent (no None/corrupted entries)
                        assert ev.event_type is not None
                except Exception as exc:
                    errors.append(exc)
                    return

        writers = [threading.Thread(target=writer, args=(tid,)) for tid in range(5)]
        readers = [threading.Thread(target=reader) for _ in range(5)]
        all_threads = writers + readers
        for t in all_threads:
            t.start()
        for t in readers:
            t.join()
        stop_flag.set()
        for t in writers:
            t.join()

        assert errors == [], f"Race condition raised: {errors[0]}"

    def test_emit_same_stop_reason_10_threads_dedup_under_concurrency(self) -> None:
        """10 threads emitting the same (stop_reason, detail, request_id) -- stored once."""
        log = _ChainEventLog()
        barrier = threading.Barrier(10)

        def worker() -> None:
            barrier.wait()
            for _ in range(50):
                log.emit_chain_event("timeout", "same-detail", "same-req-id")

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads emit the identical event -- dedup must reduce to exactly 1
        assert len(log) == 1

    def test_append_and_snapshot_interleaved_no_runtime_error(self) -> None:
        """Rapid interleaving of append + snapshot must never raise RuntimeError."""
        log = _ChainEventLog()
        errors: list[Exception] = []
        stop_flag = threading.Event()

        def appender() -> None:
            i = 0
            while not stop_flag.is_set():
                try:
                    log.append(_make_event(event_type=f"A{i}", request_id=str(i)))
                    i += 1
                except Exception as exc:
                    errors.append(exc)
                    return

        def snapshotter() -> None:
            for _ in range(500):
                try:
                    result = log.snapshot()
                    assert isinstance(result, list)
                except RuntimeError as exc:
                    # RuntimeError from "dictionary changed size during iteration"
                    # or "list changed size during iteration" -- must never happen
                    errors.append(exc)
                    return
                except Exception as exc:
                    errors.append(exc)
                    return

        appender_thread = threading.Thread(target=appender)
        snapshotter_thread = threading.Thread(target=snapshotter)
        appender_thread.start()
        snapshotter_thread.start()
        snapshotter_thread.join()
        stop_flag.set()
        appender_thread.join()

        assert errors == [], f"Interleaved append/snapshot raised: {errors[0]}"


# ---------------------------------------------------------------------------
# Adversarial: Corrupted Input
# ---------------------------------------------------------------------------


class TestAdversarialChainEventLogCorrupted:
    """Attacker mindset: malformed / edge-case inputs must not crash or bypass dedup."""

    def test_safety_event_with_none_request_id_dedup_works(self) -> None:
        """SafetyEvent with request_id=None -- dedup key handles None correctly."""
        log = _ChainEventLog()
        e1 = SafetyEvent(
            event_type="NULL_REQ",
            decision=Decision.HALT,
            reason="test",
            hook="TestHook",
            request_id=None,
        )
        e2 = SafetyEvent(
            event_type="NULL_REQ",
            decision=Decision.HALT,
            reason="test",
            hook="TestHook",
            request_id=None,
        )
        log.append(e1)
        log.append(e2)
        # Both share the same 5-tuple (including None) -- must dedup to 1
        assert len(log) == 1

    def test_safety_event_with_none_request_id_differs_from_non_none(self) -> None:
        """None request_id and non-None request_id are distinct dedup keys."""
        log = _ChainEventLog()
        e_none = SafetyEvent(
            event_type="T",
            decision=Decision.HALT,
            reason="r",
            hook="h",
            request_id=None,
        )
        e_str = SafetyEvent(
            event_type="T",
            decision=Decision.HALT,
            reason="r",
            hook="h",
            request_id="req-1",
        )
        log.append(e_none)
        log.append(e_str)
        assert len(log) == 2

    def test_safety_event_with_empty_strings_stored_correctly(self) -> None:
        """SafetyEvent with all empty string fields -- stored without error."""
        log = _ChainEventLog()
        ev = SafetyEvent(
            event_type="",
            decision=Decision.HALT,
            reason="",
            hook="",
            request_id="",
        )
        log.append(ev)
        assert len(log) == 1
        snap = log.snapshot()
        assert snap[0].event_type == ""
        assert snap[0].reason == ""
        assert snap[0].hook == ""
        assert snap[0].request_id == ""

    def test_empty_string_events_deduplicate_correctly(self) -> None:
        """Two identical empty-string events -- dedup must prevent double storage."""
        log = _ChainEventLog()
        for _ in range(5):
            log.append(
                SafetyEvent(
                    event_type="",
                    decision=Decision.HALT,
                    reason="",
                    hook="",
                    request_id="",
                )
            )
        assert len(log) == 1

    def test_emit_chain_event_unknown_stop_reason_falls_back_to_upper(self) -> None:
        """Unknown stop_reason not in _STOP_REASON_EVENT_TYPE -- fallback .upper() applied."""
        log = _ChainEventLog()
        log.emit_chain_event("some_unknown_reason", "detail", "req-1")
        snap = log.snapshot()
        assert len(snap) == 1
        # Fallback: stop_reason.upper() -- NOT stop_reason as-is
        assert snap[0].event_type == "SOME_UNKNOWN_REASON"

    def test_emit_chain_event_lowercase_unknown_reason_uppercased(self) -> None:
        """emit_chain_event with a multi-word unknown reason -- fully uppercased."""
        log = _ChainEventLog()
        log.emit_chain_event("my_custom_stop_reason", "x", "req-x")
        snap = log.snapshot()
        assert snap[0].event_type == "MY_CUSTOM_STOP_REASON"

    def test_safety_event_with_very_long_event_type_no_truncation(self) -> None:
        """SafetyEvent with a 10,000-char event_type -- stored without truncation."""
        log = _ChainEventLog()
        long_type = "E" * 10_000
        ev = _make_event(event_type=long_type, request_id="req-long")
        log.append(ev)
        assert len(log) == 1
        snap = log.snapshot()
        # Must be stored verbatim -- no silent truncation
        assert snap[0].event_type == long_type
        assert len(snap[0].event_type) == 10_000

    def test_safety_event_long_event_type_dedup_uses_full_key(self) -> None:
        """Two events with same 10K event_type -- dedup key uses full string, not truncated."""
        log = _ChainEventLog()
        long_type = "X" * 10_000
        ev1 = _make_event(event_type=long_type, request_id="req-1")
        ev2 = _make_event(event_type=long_type, request_id="req-1")
        log.append(ev1)
        log.append(ev2)
        assert len(log) == 1

    def test_safety_event_long_event_type_vs_slightly_different_not_deduped(
        self,
    ) -> None:
        """Two 10K event_type strings that differ by 1 char are distinct dedup keys."""
        log = _ChainEventLog()
        base = "Y" * 9_999
        ev1 = _make_event(event_type=base + "A", request_id="req-1")
        ev2 = _make_event(event_type=base + "B", request_id="req-1")
        log.append(ev1)
        log.append(ev2)
        assert len(log) == 2
