"""Tests for CircuitBreaker — including HALF_OPEN concurrency guard."""

from __future__ import annotations

import threading


from veronica_core.circuit_breaker import CircuitBreaker, CircuitState
from veronica_core.runtime_policy import PolicyContext


def _make_context() -> PolicyContext:
    return PolicyContext()


class TestCircuitBreakerBasic:
    def test_initial_state_is_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED

    def test_check_allows_when_closed(self):
        cb = CircuitBreaker()
        decision = cb.check(_make_context())
        assert decision.allowed

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_check_denies_when_open(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        decision = cb.check(_make_context())
        assert not decision.allowed

    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
        cb.record_failure()
        # Force state machine to re-evaluate
        state = cb.state
        assert state == CircuitState.HALF_OPEN

    def test_success_in_half_open_closes_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
        cb.record_failure()
        cb.check(_make_context())  # consumes the half-open slot
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_failure_in_half_open_reopens_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
        cb.record_failure()
        # Manually set to HALF_OPEN to avoid recovery_timeout=0 re-transition
        with cb._lock:
            cb._state = CircuitState.HALF_OPEN
        cb.check(_make_context())  # consumes the half-open slot
        cb.record_failure()
        # Should be OPEN; use _state directly to avoid _maybe_half_open_locked re-transition
        with cb._lock:
            assert cb._state == CircuitState.OPEN


class TestCircuitBreakerHalfOpenConcurrency:
    """HALF_OPEN must allow exactly one request; subsequent concurrent checks denied."""

    def test_only_first_check_passes_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
        cb.record_failure()
        # transition to HALF_OPEN
        _ = cb.state

        ctx = _make_context()
        first = cb.check(ctx)
        second = cb.check(ctx)

        assert first.allowed, "First check should be allowed in HALF_OPEN"
        assert not second.allowed, "Second concurrent check must be denied in HALF_OPEN"
        assert "already in flight" in (second.reason or "")

    def test_concurrent_threads_only_one_passes(self):
        """Under real thread concurrency, only one thread should get ALLOW."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
        cb.record_failure()
        # Ensure HALF_OPEN state
        _ = cb.state

        results: list[bool] = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            decision = cb.check(_make_context())
            results.append(decision.allowed)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        allowed_count = sum(results)
        assert allowed_count == 1, (
            f"Expected exactly 1 allowed in HALF_OPEN under concurrency, got {allowed_count}"
        )

    def test_in_flight_reset_after_record_success(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
        cb.record_failure()
        _ = cb.state

        cb.check(_make_context())  # slot consumed
        cb.record_success()        # resets in_flight + closes circuit
        assert cb._half_open_in_flight == 0

    def test_in_flight_reset_after_record_failure(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
        cb.record_failure()
        _ = cb.state

        cb.check(_make_context())  # slot consumed
        cb.record_failure()        # resets in_flight + reopens
        assert cb._half_open_in_flight == 0
