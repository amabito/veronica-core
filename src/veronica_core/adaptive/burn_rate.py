"""BurnRateEstimator — Sliding-window cost burn rate for predictive budget control.

Tracks a sliding window of cost events and provides:
  - current_rate(): cost per second within the window
  - time_to_exhaustion(): seconds until budget is depleted at current rate
  - projected_cost(): estimated cumulative cost over a future horizon

EMA (Exponential Moving Average) is applied for trend weighting.
Thread-safe via threading.Lock.
No external dependencies (stdlib only).
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Optional


class BurnRateEstimator:
    """Sliding-window burn rate estimator with EMA trend weighting.

    Records cost events and computes the current burn rate (cost/second)
    using a configurable sliding window.  An EMA smooths individual
    rate samples to dampen transient spikes.

    Thread-safe: all public methods acquire self._lock.

    Args:
        alpha: EMA smoothing factor in (0, 1].  Higher values give more
            weight to recent samples.  Default 0.3.
        max_window_size: Maximum number of events retained in the
            sliding window.  Oldest events are dropped when exceeded.
            Default 10000.
    """

    def __init__(
        self,
        alpha: float = 0.3,
        max_window_size: int = 10_000,
    ) -> None:
        if not (0 < alpha <= 1.0):
            raise ValueError(f"alpha must be in (0, 1.0], got {alpha}")
        if max_window_size < 1:
            raise ValueError(f"max_window_size must be >= 1, got {max_window_size}")

        self._alpha = alpha
        self._max_window_size = max_window_size
        # deque with maxlen enforces the size bound automatically;
        # when full, appending drops the oldest element from the left.
        self._events: deque[tuple[float, float]] = deque(maxlen=max_window_size)
        self._total_cost: float = 0.0  # running total, O(1) maintenance
        self._ema_rate: Optional[float] = None  # EMA of cost/second
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, cost: float, timestamp: Optional[float] = None) -> None:
        """Record a cost event.

        Silently ignores NaN or Inf cost values.

        Args:
            cost: Cost amount to record.  NaN/Inf values are ignored.
            timestamp: Monotonic timestamp.  Defaults to time.monotonic().
        """
        if not math.isfinite(cost) or cost < 0.0:
            return
        ts = timestamp if timestamp is not None else time.monotonic()
        with self._lock:
            # If the deque is already at capacity the oldest element is evicted
            # automatically by deque(maxlen=...).  Subtract its cost first.
            if len(self._events) == self._max_window_size:
                self._total_cost -= self._events[0][1]
            self._events.append((ts, cost))
            self._total_cost += cost
            self._update_ema_locked()

    def current_rate(self, window_sec: float = 3600.0) -> float:
        """Compute cost per second within the sliding window.

        Args:
            window_sec: Window duration in seconds.

        Returns:
            Cost per second.  0.0 if no events fall within the window
            or the window spans zero time.
        """
        if window_sec <= 0:
            return 0.0
        now = time.monotonic()
        cutoff = now - window_sec
        with self._lock:
            return self._rate_in_window_locked(cutoff, now)

    def current_rates(self, windows_sec: list[float]) -> list[float]:
        """Compute cost per second for multiple windows in a single lock acquisition.

        This avoids snapshot inconsistency when the caller needs rates from
        several windows (e.g. 60s and 3600s) at the same point in time.

        Args:
            windows_sec: List of window durations in seconds.

        Returns:
            List of rates, one per window, in the same order as *windows_sec*.
        """
        now = time.monotonic()
        with self._lock:
            return [
                self._rate_in_window_locked(now - w, now) if w > 0 else 0.0
                for w in windows_sec
            ]

    def time_to_exhaustion(self, remaining_budget: float) -> Optional[float]:
        """Seconds until remaining_budget is depleted at the current burn rate.

        Args:
            remaining_budget: Remaining budget in the same unit as costs.

        Returns:
            Seconds until exhaustion, or None if burn rate is zero.
        """
        rate = self.current_rate()
        if rate <= 0.0:
            return None
        if remaining_budget <= 0.0:
            return 0.0
        return remaining_budget / rate

    def projected_cost(self, horizon_sec: float) -> float:
        """Estimated cumulative cost over the given horizon.

        Uses the EMA-smoothed rate for projection.  Falls back to
        the raw rate if no EMA is available.

        Args:
            horizon_sec: Future duration in seconds.

        Returns:
            Projected cost.  0.0 if no data.
        """
        if horizon_sec <= 0.0:
            return 0.0
        with self._lock:
            if self._ema_rate is not None and self._ema_rate > 0.0:
                return self._ema_rate * horizon_sec
            # Fallback to raw rate over last hour
            now = time.monotonic()
            cutoff = now - 3600.0
            rate = self._rate_in_window_locked(cutoff, now)
            return rate * horizon_sec

    # ------------------------------------------------------------------
    # Private helpers (caller must hold self._lock)
    # ------------------------------------------------------------------

    def _rate_in_window_locked(self, cutoff: float, now: float) -> float:
        """Compute raw cost/second for events between cutoff and now."""
        total_cost = 0.0
        for ts, cost in self._events:
            if ts >= cutoff:
                total_cost += cost
        elapsed = now - cutoff
        if elapsed <= 0.0:
            return 0.0
        return total_cost / elapsed

    def _update_ema_locked(self) -> None:
        """Recompute EMA rate using the running total (O(1) cost access)."""
        if not self._events:
            self._ema_rate = None
            return
        # Use the span of all recorded events for the instantaneous rate.
        # _total_cost is maintained incrementally — no O(n) sum() needed.
        oldest_ts = self._events[0][0]
        newest_ts = self._events[-1][0]
        span = newest_ts - oldest_ts
        if span <= 0.0:
            # All events at same timestamp: rate is undefined, skip update.
            return
        instant_rate = self._total_cost / span
        if self._ema_rate is None:
            self._ema_rate = instant_rate
        else:
            self._ema_rate = (
                self._alpha * instant_rate + (1.0 - self._alpha) * self._ema_rate
            )
