"""Tests for AnomalyDetector."""

from __future__ import annotations

import math
import threading


from veronica_core.adaptive.anomaly import AnomalyDetector


class TestAnomalyDetectorBasics:
    def test_unknown_metric_is_not_anomalous(self):
        detector = AnomalyDetector()
        assert detector.is_anomalous("foo", 999.0) is False

    def test_warmup_period_returns_false(self):
        """During warmup (n < min_samples), always False."""
        detector = AnomalyDetector(min_samples=30)
        for i in range(29):
            detector.record("metric", float(i))
        assert detector.sample_count("metric") == 29
        assert detector.is_anomalous("metric", 9999.0) is False

    def test_after_warmup_outlier_detected(self):
        """5-sigma outlier detected after warmup."""
        detector = AnomalyDetector(min_samples=30)
        # Record normal distribution centered at 0 with std ≈ 1
        for i in range(100):
            detector.record("latency", float(i % 10))
        mean, std, n = detector.stats("latency")
        assert n == 100
        # Inject a 5-sigma outlier
        outlier = mean + 5.0 * std
        assert detector.is_anomalous("latency", outlier, z_threshold=3.0) is True

    def test_normal_value_not_anomalous(self):
        """Value within 2 sigma is not anomalous with threshold=3."""
        detector = AnomalyDetector(min_samples=10)
        for i in range(50):
            detector.record("cpu", 50.0 + (i % 5) * 0.1)
        _, std, _ = detector.stats("cpu")
        normal_value = 50.0 + std  # ~1-sigma
        assert detector.is_anomalous("cpu", normal_value, z_threshold=3.0) is False

    def test_multiple_metrics_tracked_independently(self):
        """Different metrics do not interfere with each other."""
        detector = AnomalyDetector(min_samples=10)
        for i in range(50):
            detector.record("metric_a", 1.0)
            detector.record("metric_b", 100.0)
        mean_a, _, _ = detector.stats("metric_a")
        mean_b, _, _ = detector.stats("metric_b")
        assert abs(mean_a - 1.0) < 1e-9
        assert abs(mean_b - 100.0) < 1e-9
        # A value normal for metric_b is anomalous for metric_a
        # metric_a std is 0 (constant), so any deviation would be infinite z-score
        # But std=0 returns False (guard in is_anomalous)
        assert detector.is_anomalous("metric_a", 100.0, z_threshold=3.0) is False

    def test_metric_with_std_zero_returns_false(self):
        """Constant metric (std=0) never reports anomaly."""
        detector = AnomalyDetector(min_samples=5)
        for _ in range(30):
            detector.record("constant", 42.0)
        assert detector.is_anomalous("constant", 43.0) is False

    def test_sample_count_returns_correct_count(self):
        detector = AnomalyDetector(min_samples=5)
        for i in range(15):
            detector.record("x", float(i))
        assert detector.sample_count("x") == 15
        assert detector.sample_count("unknown") == 0


class TestWelfordNumericalStability:
    def test_large_offset_values(self):
        """Welford's algorithm should be numerically stable with large offsets."""
        detector = AnomalyDetector(min_samples=10)
        base = 1e9
        # Values: base + tiny variation
        for i in range(100):
            detector.record("large_offset", base + (i % 10) * 0.001)
        mean, std, n = detector.stats("large_offset")
        assert n == 100
        assert abs(mean - (base + 4.5 * 0.001)) < 1.0  # mean close to base
        assert std < 1.0  # std should be tiny, not dominated by cancellation error

    def test_welford_matches_expected_mean_std(self):
        """Verify Welford's mean and std on a simple known dataset."""
        # Dataset: [1, 2, 3, 4, 5] — mean=3, sample_std=sqrt(2.5)≈1.581
        detector = AnomalyDetector(min_samples=5)
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            detector.record("known", v)
        mean, std, n = detector.stats("known")
        assert n == 5
        assert abs(mean - 3.0) < 1e-9
        assert abs(std - math.sqrt(2.5)) < 1e-9


class TestAnomalyDetectorThreadSafety:
    def test_concurrent_record_and_check(self):
        """Concurrent record() and is_anomalous() calls should not corrupt state."""
        detector = AnomalyDetector(min_samples=30)
        # Pre-fill enough for warmup
        for i in range(30):
            detector.record("concurrent", float(i))

        errors = []

        def recorder() -> None:
            try:
                for i in range(200):
                    detector.record("concurrent", float(i % 50))
            except Exception as e:
                errors.append(e)

        def checker() -> None:
            try:
                for _ in range(200):
                    detector.is_anomalous("concurrent", 25.0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=recorder) for _ in range(5)] + [
            threading.Thread(target=checker) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"
        # n should be 30 + 5*200 = 1030
        assert detector.sample_count("concurrent") == 1030

    def test_concurrent_new_metrics(self):
        """Multiple threads creating new metrics simultaneously should not deadlock."""
        detector = AnomalyDetector(min_samples=5)
        errors = []

        def create_metric(thread_id: int) -> None:
            try:
                for i in range(20):
                    detector.record(f"metric_{thread_id}", float(i))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_metric, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        for i in range(20):
            assert detector.sample_count(f"metric_{i}") == 20
