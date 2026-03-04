"""AnomalyDetector — Per-metric Z-score anomaly detection.

Uses Welford's online algorithm for numerically stable running mean
and variance.  No NumPy, no external dependencies.

Can be used as a PreDispatchHook in ShieldPipeline by wrapping check()
calls — or directly via record() / is_anomalous().

Thread-safe: one Lock per metric, created lazily.
"""

from __future__ import annotations

import math
import threading


class _MetricState:
    """Welford's online algorithm state for a single metric."""

    __slots__ = ("n", "mean", "M2", "lock")

    def __init__(self) -> None:
        self.n: int = 0
        self.mean: float = 0.0
        self.M2: float = 0.0  # sum of squared deviations
        self.lock: threading.Lock = threading.Lock()

    def update(self, value: float) -> None:
        """Welford online update — call with self.lock held or externally."""
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        delta2 = value - self.mean
        self.M2 += delta * delta2

    @property
    def variance(self) -> float:
        """Sample variance (n-1 denominator).  0 if n < 2."""
        if self.n < 2:
            return 0.0
        return self.M2 / (self.n - 1)

    @property
    def std(self) -> float:
        """Sample standard deviation."""
        return math.sqrt(self.variance)


class AnomalyDetector:
    """Z-score based anomaly detector for arbitrary named metrics.

    Tracks a running mean and standard deviation per metric using
    Welford's online algorithm (numerically stable, no NumPy).

    Warmup period: until ``min_samples`` observations have been recorded
    for a metric, ``is_anomalous()`` always returns ``False``.

    Thread-safe: each metric has its own Lock.

    Args:
        min_samples: Minimum observations before anomaly detection
            activates for a metric.  Default 30.
    """

    def __init__(self, min_samples: int = 30) -> None:
        if min_samples < 1:
            raise ValueError(f"min_samples must be >= 1, got {min_samples}")
        self._min_samples = min_samples
        self._metrics: dict[str, _MetricState] = {}
        self._registry_lock = threading.Lock()  # protects _metrics dict writes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, metric_name: str, value: float) -> None:
        """Record an observation for the named metric.

        Silently ignores NaN and Inf values to prevent state corruption.

        Args:
            metric_name: Metric identifier.
            value: Observed value.  NaN/Inf are skipped.
        """
        if not math.isfinite(value):
            return
        state = self._get_or_create(metric_name)
        with state.lock:
            state.update(value)

    def is_anomalous(
        self,
        metric_name: str,
        value: float,
        z_threshold: float = 3.0,
    ) -> bool:
        """Check if value is anomalous for the named metric.

        Returns False during warmup (n < min_samples) or if the metric
        has never been recorded.  Returns False for NaN/Inf values.

        Args:
            metric_name: Metric identifier.
            value: Value to evaluate.
            z_threshold: Z-score threshold above which the value is
                considered anomalous.  Default 3.0.

        Returns:
            True if |z-score| > z_threshold and n >= min_samples.
        """
        if not math.isfinite(value):
            return False
        state = self._get_state(metric_name)
        if state is None:
            return False
        with state.lock:
            if state.n < self._min_samples:
                return False
            sigma = state.std
            if sigma <= 0.0:
                return False
            z_score = abs(value - state.mean) / sigma
            return z_score > z_threshold

    def sample_count(self, metric_name: str) -> int:
        """Return the number of observations recorded for a metric.

        Returns 0 if the metric has not been seen.

        Args:
            metric_name: Metric identifier.
        """
        state = self._get_state(metric_name)
        if state is None:
            return 0
        with state.lock:
            return state.n

    def stats(self, metric_name: str) -> tuple[float, float, int]:
        """Return (mean, std, n) for the named metric.

        Returns (0.0, 0.0, 0) if the metric is unknown.

        Args:
            metric_name: Metric identifier.
        """
        state = self._get_state(metric_name)
        if state is None:
            return 0.0, 0.0, 0
        with state.lock:
            return state.mean, state.std, state.n

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_or_create(self, metric_name: str) -> _MetricState:
        """Return existing state or create new one (thread-safe)."""
        # Fast path: no lock if already exists.
        # Note: dict.get() is atomic under CPython's GIL.  On non-GIL
        # interpreters (PyPy, nogil) this would need the registry lock.
        state = self._metrics.get(metric_name)
        if state is not None:
            return state
        # Slow path: create under registry lock
        with self._registry_lock:
            state = self._metrics.get(metric_name)
            if state is None:
                state = _MetricState()
                self._metrics[metric_name] = state
            return state

    def _get_state(self, metric_name: str) -> _MetricState | None:
        """Return state for metric_name or None if not recorded.

        Note: dict.get() is atomic under CPython's GIL.  On non-GIL
        interpreters this would need the registry lock.
        """
        return self._metrics.get(metric_name)
