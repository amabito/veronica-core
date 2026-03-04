"""Adversarial tests for the adaptive policy module.

Attacker mindset: how can these components be broken?

Coverage checklist (team-lead requirements):
  [x] 1. NaN/Inf/negative cost values in BurnRateEstimator → no crash
  [x] 2. Concurrent record() + current_rate() from 10+ threads → consistent
  [x] 3. Zero/negative remaining_budget → immediate HALT, no division by zero
  [x] 4. Rapid spike/normal alternation → no state oscillation
  [x] 5. Extremely large window (1M records) → bounded memory
  [x] 6. time.monotonic() going backwards (mocked) → handled
  [x] 7. AnomalyDetector: all-same values (zero variance) → no division by zero
  [x] 8. AnomalyDetector: single huge outlier after warmup → detected
"""

from __future__ import annotations

import math
import threading
import time
from unittest.mock import patch

import pytest

from veronica_core.adaptive.anomaly import AnomalyDetector
from veronica_core.adaptive.burn_rate import BurnRateEstimator
from veronica_core.adaptive.threshold import AdaptiveConfig, AdaptiveThresholdPolicy
from veronica_core.runtime_policy import PolicyContext


class TestNaNInfHandling:
    def test_nan_cost_ignored_by_burn_rate(self):
        """NaN cost must not crash or corrupt BurnRateEstimator state."""
        est = BurnRateEstimator()
        est.record(1.0, timestamp=1000.0)
        est.record(float("nan"), timestamp=1001.0)
        est.record(1.0, timestamp=1002.0)
        # Only 2 valid events should be recorded
        assert len(est._events) == 2
        rate = est.current_rate()
        assert math.isfinite(rate)

    def test_inf_cost_ignored_by_burn_rate(self):
        """Inf cost must not crash or corrupt BurnRateEstimator state."""
        est = BurnRateEstimator()
        est.record(1.0, timestamp=1000.0)
        est.record(float("inf"), timestamp=1001.0)
        est.record(-float("inf"), timestamp=1002.0)
        assert len(est._events) == 1
        rate = est.current_rate()
        assert math.isfinite(rate)

    def test_nan_cost_in_policy_check(self):
        """NaN in PolicyContext.cost_usd must not crash AdaptiveThresholdPolicy."""
        est = BurnRateEstimator()
        policy = AdaptiveThresholdPolicy(
            burn_rate=est, remaining_budget=100.0
        )
        ctx = PolicyContext(cost_usd=float("nan"))
        decision = policy.check(ctx)
        # Should return a valid decision without crashing
        assert isinstance(decision.allowed, bool)

    def test_nan_metric_ignored_by_anomaly_detector(self):
        """NaN values should be silently ignored in AnomalyDetector."""
        detector = AnomalyDetector(min_samples=5)
        for i in range(30):
            detector.record("metric", float(i))
        count_before = detector.sample_count("metric")
        detector.record("metric", float("nan"))
        detector.record("metric", float("inf"))
        # NaN/Inf should not be counted
        assert detector.sample_count("metric") == count_before

    def test_nan_in_is_anomalous_returns_false(self):
        """NaN value passed to is_anomalous() should return False, not crash."""
        detector = AnomalyDetector(min_samples=5)
        for i in range(30):
            detector.record("m", float(i))
        assert detector.is_anomalous("m", float("nan")) is False
        assert detector.is_anomalous("m", float("inf")) is False


class TestNegativeTimestamps:
    def test_negative_timestamp_handled(self):
        """Negative timestamps should not crash BurnRateEstimator."""
        est = BurnRateEstimator()
        est.record(1.0, timestamp=-1000.0)
        est.record(1.0, timestamp=-500.0)
        est.record(1.0, timestamp=0.0)
        # projected_cost and current_rate should not crash
        rate = est.current_rate(window_sec=9999.0)
        assert math.isfinite(rate)
        proj = est.projected_cost(3600.0)
        assert math.isfinite(proj)


class TestHysteresisNoOscillation:
    def test_rapid_alternating_spike_no_crash(self):
        """Rapid alternating spike/normal should not crash or infinite-loop."""
        cfg = AdaptiveConfig(
            spike_multiplier=2.0,
            warn_at_exhaustion_hours=24.0,
            degrade_at_exhaustion_hours=6.0,
            halt_at_exhaustion_hours=1.0,
        )
        est = BurnRateEstimator()
        policy = AdaptiveThresholdPolicy(
            burn_rate=est, remaining_budget=1_000_000.0, config=cfg
        )
        now = time.monotonic()
        # Baseline
        for i in range(60):
            est.record(0.01, timestamp=now - 3600 + i * 60)

        ctx = PolicyContext()
        decisions = []
        for i in range(20):
            # Alternately inject spike and normal
            if i % 2 == 0:
                est.record(100.0, timestamp=now + i * 0.1)
            else:
                est.record(0.001, timestamp=now + i * 0.1)
            d = policy.check(ctx)
            decisions.append(d)

        # All decisions should be valid PolicyDecision objects
        for d in decisions:
            assert isinstance(d.allowed, bool)
            assert d.policy_type == "adaptive_threshold"

    def test_no_oscillation_pattern(self):
        """The policy must not oscillate between states on stable input."""
        cfg = AdaptiveConfig(
            spike_multiplier=100.0,  # disable spike detection
        )
        est = BurnRateEstimator()
        policy = AdaptiveThresholdPolicy(
            burn_rate=est, remaining_budget=1_000_000.0, config=cfg
        )
        now = time.monotonic()
        # Stable low rate
        for i in range(60):
            est.record(0.001, timestamp=now - 3600 + i * 60)
        ctx = PolicyContext()
        # 10 consecutive checks should give consistent results
        results = [policy.check(ctx).allowed for _ in range(10)]
        assert all(r == results[0] for r in results), (
            f"Oscillation detected: {results}"
        )


class TestZeroRemainingBudgetImmediate:
    def test_zero_remaining_halts_immediately(self):
        """Zero remaining_budget → HALT even without burn rate data."""
        est = BurnRateEstimator()
        policy = AdaptiveThresholdPolicy(
            burn_rate=est, remaining_budget=0.0
        )
        ctx = PolicyContext()
        decision = policy.check(ctx)
        assert decision.allowed is False
        assert "exhausted" in decision.reason.lower()

    def test_negative_remaining_halts(self):
        """Negative remaining budget after context cost subtraction → HALT."""
        est = BurnRateEstimator()
        # remaining=0.5, context deducts 1.0 → remaining goes to 0
        policy = AdaptiveThresholdPolicy(
            burn_rate=est, remaining_budget=0.5
        )
        ctx = PolicyContext(cost_usd=1.0)
        decision = policy.check(ctx)
        assert decision.allowed is False

    def test_update_remaining_to_zero_halts(self):
        """update_remaining_budget(0) → subsequent check returns HALT."""
        est = BurnRateEstimator()
        policy = AdaptiveThresholdPolicy(
            burn_rate=est, remaining_budget=100.0
        )
        policy.update_remaining_budget(0.0)
        ctx = PolicyContext()
        decision = policy.check(ctx)
        assert decision.allowed is False


# ---------------------------------------------------------------------------
# Requirement 2: Concurrent record() + current_rate() from 10+ threads
# ---------------------------------------------------------------------------

class TestConcurrentRecordAndRate:
    def test_10_threads_record_and_rate_consistent(self):
        """10 threads recording and reading simultaneously: no crash, finite rate."""
        est = BurnRateEstimator()
        errors: list[Exception] = []
        rates: list[float] = []
        lock = threading.Lock()

        def recorder(tid: int) -> None:
            try:
                now = time.monotonic()
                for i in range(100):
                    est.record(0.01, timestamp=now - 100 + i + tid * 0.001)
            except Exception as e:
                with lock:
                    errors.append(e)

        def reader() -> None:
            try:
                for _ in range(50):
                    r = est.current_rate(window_sec=3600.0)
                    with lock:
                        rates.append(r)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = (
            [threading.Thread(target=recorder, args=(i,)) for i in range(10)]
            + [threading.Thread(target=reader) for _ in range(5)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        # Every rate sample must be finite and non-negative
        for r in rates:
            assert math.isfinite(r), f"Non-finite rate: {r}"
            assert r >= 0.0, f"Negative rate: {r}"

    def test_concurrent_tte_never_crashes(self):
        """Concurrent time_to_exhaustion() calls with active recording: no crash."""
        est = BurnRateEstimator()
        errors: list[Exception] = []
        now = time.monotonic()
        # Pre-fill some data
        for i in range(50):
            est.record(1.0, timestamp=now - 3600 + i * 72)

        def recorder() -> None:
            try:
                for i in range(100):
                    est.record(0.5, timestamp=time.monotonic())
            except Exception as e:
                errors.append(e)

        def tte_reader() -> None:
            try:
                for _ in range(100):
                    result = est.time_to_exhaustion(500.0)
                    assert result is None or (math.isfinite(result) and result >= 0.0)
            except Exception as e:
                errors.append(e)

        threads = (
            [threading.Thread(target=recorder) for _ in range(5)]
            + [threading.Thread(target=tte_reader) for _ in range(5)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors: {errors}"


# ---------------------------------------------------------------------------
# Requirement 5: Extremely large window (1M records) → bounded memory
# ---------------------------------------------------------------------------

class TestLargeWindowMemoryBound:
    def test_1m_records_bounded_by_max_window_size(self):
        """Recording 1M events must not exceed max_window_size in memory."""
        max_size = 1000
        est = BurnRateEstimator(max_window_size=max_size)
        now = time.monotonic()
        for i in range(1_000_000):
            est.record(0.001, timestamp=now + i * 0.001)
        assert len(est._events) == max_size
        # oldest event should be the 999_001-th (0-indexed)
        oldest_ts = est._events[0][0]
        assert oldest_ts == pytest.approx(now + 999_000 * 0.001, abs=1e-6)

    def test_default_max_window_size_is_10000(self):
        """Default max_window_size=10000: 20000 records → deque has 10000."""
        est = BurnRateEstimator()
        now = time.monotonic()
        for i in range(20_000):
            est.record(0.001, timestamp=now + i * 0.001)
        assert len(est._events) == 10_000

    def test_rate_still_finite_after_large_fill(self):
        """current_rate() must return a finite value after max-window fill."""
        est = BurnRateEstimator(max_window_size=500)
        now = time.monotonic()
        for i in range(10_000):
            est.record(1.0, timestamp=now - 3600 + i * 0.36)
        rate = est.current_rate(window_sec=3600.0)
        assert math.isfinite(rate)
        assert rate >= 0.0


# ---------------------------------------------------------------------------
# Requirement 6: time.monotonic() going backwards (mocked) → handled
# ---------------------------------------------------------------------------

class TestMonotonicBackwards:
    def test_backwards_monotonic_in_record(self):
        """If time.monotonic() somehow returns a past value, record() must not crash."""
        est = BurnRateEstimator()
        # Inject events with decreasing timestamps (simulating clock backwards)
        for i in range(10):
            est.record(1.0, timestamp=1000.0 - i * 0.1)
        # current_rate and projected_cost must not raise
        rate = est.current_rate(window_sec=99999.0)
        assert math.isfinite(rate)

    def test_backwards_monotonic_ema_no_crash(self):
        """EMA update with backwards timestamps: span<=0 is guarded, no crash."""
        est = BurnRateEstimator()
        # All events at the exact same timestamp → span=0, EMA update is skipped
        ts = time.monotonic()
        for _ in range(10):
            est.record(1.0, timestamp=ts)
        # _ema_rate may be None or some value — must not be NaN/Inf
        with est._lock:
            if est._ema_rate is not None:
                assert math.isfinite(est._ema_rate)

    def test_current_rate_negative_window_sec_returns_zero(self):
        """window_sec <= 0 should return 0.0, not raise."""
        est = BurnRateEstimator()
        est.record(1.0, timestamp=time.monotonic())
        assert est.current_rate(window_sec=0.0) == 0.0
        assert est.current_rate(window_sec=-100.0) == 0.0

    def test_mocked_monotonic_backwards_does_not_crash(self):
        """Patch time.monotonic to go backwards between calls: no crash."""
        est = BurnRateEstimator()
        call_count = 0

        def fake_monotonic() -> float:
            nonlocal call_count
            call_count += 1
            # Returns 1000, 999, 998, ... (backwards)
            return 1000.0 - call_count

        with patch("veronica_core.adaptive.burn_rate.time.monotonic", fake_monotonic):
            for _ in range(5):
                est.record(1.0)  # uses fake_monotonic for timestamp
            rate = est.current_rate(window_sec=3600.0)  # uses fake_monotonic for now
        assert math.isfinite(rate)
        assert rate >= 0.0


# ---------------------------------------------------------------------------
# Requirements 7 & 8: AnomalyDetector edge cases
# ---------------------------------------------------------------------------

class TestAnomalyDetectorEdgeCases:
    def test_zero_variance_no_division_by_zero(self):
        """All-identical values → std=0 → is_anomalous returns False, no ZeroDivisionError."""
        detector = AnomalyDetector(min_samples=5)
        for _ in range(50):
            detector.record("constant", 42.0)
        # std is 0; must not raise ZeroDivisionError
        result = detector.is_anomalous("constant", 42.0)
        assert result is False
        result2 = detector.is_anomalous("constant", 1_000_000.0)
        assert result2 is False  # std=0 → z = inf/0, guarded

    def test_single_huge_outlier_detected_after_warmup(self):
        """After warmup with normal values, a 10-sigma outlier must be detected."""
        detector = AnomalyDetector(min_samples=30)
        # Build a stable distribution: mean=0, std≈1
        import random
        rng = random.Random(42)
        for _ in range(100):
            detector.record("signal", rng.gauss(0.0, 1.0))

        mean, std, n = detector.stats("signal")
        assert n == 100
        assert std > 0.0

        # Single 10-sigma outlier
        outlier = mean + 10.0 * std
        assert detector.is_anomalous("signal", outlier, z_threshold=3.0) is True

    def test_outlier_just_below_threshold_not_flagged(self):
        """Value at exactly z_threshold - epsilon should NOT be flagged."""
        detector = AnomalyDetector(min_samples=5)
        for i in range(50):
            detector.record("m", float(i % 10))
        mean, std, _ = detector.stats("m")
        if std <= 0.0:
            return  # skip if degenerate
        # Value that yields z = 2.99 (below threshold of 3.0)
        value = mean + 2.99 * std
        assert detector.is_anomalous("m", value, z_threshold=3.0) is False

    def test_negative_cost_in_burn_rate(self):
        """Negative cost values should still be recorded (they are finite)."""
        est = BurnRateEstimator()
        # Negative costs are unusual but finite — record should not crash
        now = time.monotonic()
        est.record(-1.0, timestamp=now - 60)
        est.record(2.0, timestamp=now)
        rate = est.current_rate(window_sec=120.0)
        assert math.isfinite(rate)


# ---------------------------------------------------------------------------
# Explicit named tests requested by team-lead
# (logic exercised above; these provide unambiguous named coverage)
# ---------------------------------------------------------------------------

class TestExplicitAdversarialRequirements:
    def test_concurrent_record_and_current_rate_no_crash(self):
        """10 threads doing record() + current_rate() simultaneously → no crash, consistent state."""
        est = BurnRateEstimator()
        errors: list[Exception] = []
        results: list[float] = []
        lock = threading.Lock()
        now = time.monotonic()

        def do_record(tid: int) -> None:
            try:
                for i in range(50):
                    est.record(0.01, timestamp=now - 60 + i * 1.2 + tid * 0.01)
            except Exception as e:
                with lock:
                    errors.append(e)

        def do_rate() -> None:
            try:
                for _ in range(50):
                    r = est.current_rate(window_sec=3600.0)
                    with lock:
                        results.append(r)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = (
            [threading.Thread(target=do_record, args=(i,)) for i in range(10)]
            + [threading.Thread(target=do_rate) for _ in range(5)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent errors: {errors}"
        for r in results:
            assert math.isfinite(r) and r >= 0.0, f"Bad rate value: {r}"

    def test_bounded_memory_100k_events(self):
        """100K record() calls → len(est._events) <= max_window_size."""
        max_size = 500
        est = BurnRateEstimator(max_window_size=max_size)
        now = time.monotonic()
        for i in range(100_000):
            est.record(0.001, timestamp=now + i * 0.001)
        assert len(est._events) <= max_size
        assert len(est._events) == max_size

    def test_zero_variance_anomaly_detector_no_division_by_zero(self):
        """AnomalyDetector with all-same values: is_anomalous(5.0) → False, no ZeroDivisionError."""
        detector = AnomalyDetector(min_samples=5)
        for _ in range(50):
            detector.record("m", 5.0)
        # std == 0: must not raise, must return False
        assert detector.is_anomalous("m", 5.0) is False
        assert detector.is_anomalous("m", 5.0, z_threshold=0.0) is False
