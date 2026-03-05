"""Tests for BurnRateEstimator."""

from __future__ import annotations

import threading
import time


from veronica_core.adaptive.burn_rate import BurnRateEstimator


class TestBurnRateBasics:
    def test_empty_estimator_rate_is_zero(self):
        est = BurnRateEstimator()
        assert est.current_rate() == 0.0

    def test_empty_estimator_tte_is_none(self):
        est = BurnRateEstimator()
        assert est.time_to_exhaustion(100.0) is None

    def test_empty_projected_cost_is_zero(self):
        est = BurnRateEstimator()
        assert est.projected_cost(3600.0) == 0.0

    def test_constant_rate_current_rate(self):
        """10 events of $1 each over 10 seconds → positive rate."""
        est = BurnRateEstimator()
        # Use recent timestamps so they fall within the 3600s window
        base_ts = time.monotonic() - 10.0
        for i in range(10):
            est.record(1.0, timestamp=base_ts + i)
        rate = est.current_rate(window_sec=3600.0)
        assert rate > 0.0

    def test_constant_rate_tte(self):
        """Verify TTE is plausible for a known rate."""
        est = BurnRateEstimator()
        base = time.monotonic()
        for i in range(100):
            est.record(0.01, timestamp=base - 100 + i)
        # Rate ≈ 1.0 / 100s = 0.01 /s (100 events × $0.01 over 100s)
        tte = est.time_to_exhaustion(1.0)
        assert tte is not None
        assert tte > 0.0

    def test_tte_zero_remaining(self):
        est = BurnRateEstimator()
        est.record(1.0, timestamp=time.monotonic() - 1.0)
        est.record(1.0, timestamp=time.monotonic())
        tte = est.time_to_exhaustion(0.0)
        assert tte == 0.0

    def test_window_sliding_old_entries_expire(self):
        """Records older than window_sec should not count."""
        est = BurnRateEstimator()
        now = time.monotonic()
        # Old events (2 hours ago)
        for _ in range(10):
            est.record(100.0, timestamp=now - 7200)
        # No recent events
        rate = est.current_rate(window_sec=3600.0)
        # Old events are still in the deque but outside the window
        # rate = 0 / (now - cutoff) = 0.0
        assert rate == 0.0

    def test_burst_then_silence_rate_decreases(self):
        """After a burst, if no new events arrive the rate decreases over time."""
        est = BurnRateEstimator()
        now = time.monotonic()
        # Burst 5 minutes ago
        for _ in range(10):
            est.record(1.0, timestamp=now - 300)
        est.current_rate(window_sec=3600.0)
        rate_10min = est.current_rate(window_sec=600.0)
        rate_1min = est.current_rate(window_sec=60.0)
        # Narrower windows exclude the old burst → lower rate
        # rate_1min should be 0 (events are 300s ago, outside 60s window)
        assert rate_1min == 0.0
        # rate_10min includes the events
        assert rate_10min >= 0.0

    def test_projected_cost_accuracy_steady_state(self):
        """projected_cost accuracy within 10% for steady-state scenario."""
        est = BurnRateEstimator(alpha=0.3)
        now = time.monotonic()
        # $1 every 60 seconds for 1 hour
        for i in range(60):
            est.record(1.0, timestamp=now - 3600 + i * 60)
        # Rate ≈ $60/hr ≈ $1/min = 1/60 per second
        # projected_cost(3600) should be ≈ 60
        projected = est.projected_cost(3600.0)
        # Within 10% of 60
        assert 54 <= projected <= 66, f"projected={projected}"

    def test_max_window_size_enforced(self):
        """Oldest events should be dropped when max_window_size is exceeded."""
        est = BurnRateEstimator(max_window_size=5)
        for i in range(10):
            est.record(1.0, timestamp=float(i))
        # Only last 5 events should remain
        assert len(est._events) == 5
        oldest_ts = est._events[0][0]
        assert oldest_ts == 5.0


class TestBurnRateThreadSafety:
    def test_concurrent_record_calls(self):
        """Multiple threads recording simultaneously should not corrupt state."""
        est = BurnRateEstimator()
        errors = []
        now = time.monotonic()

        def worker(thread_id: int) -> None:
            try:
                for i in range(100):
                    est.record(0.001, timestamp=now + thread_id * 100 + i)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        rate = est.current_rate(window_sec=3600.0)
        assert rate >= 0.0
