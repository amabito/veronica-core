"""Tests for ApprovalRateLimiter (Task H)."""
from __future__ import annotations

import threading
import time

import pytest

from veronica_core.approval.rate_limit import ApprovalRateLimiter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAcquire:
    def test_acquire_within_limit_returns_true(self) -> None:
        limiter = ApprovalRateLimiter(max_per_window=5, window_seconds=60.0)
        for _ in range(5):
            assert limiter.acquire() is True

    def test_acquire_exceeds_limit_returns_false(self) -> None:
        limiter = ApprovalRateLimiter(max_per_window=3, window_seconds=60.0)
        for _ in range(3):
            limiter.acquire()
        assert limiter.acquire() is False

    def test_tokens_refill_after_window(self) -> None:
        limiter = ApprovalRateLimiter(max_per_window=2, window_seconds=0.1)
        assert limiter.acquire() is True
        assert limiter.acquire() is True
        assert limiter.acquire() is False
        time.sleep(0.15)
        # Window expired: tokens should be available again
        assert limiter.acquire() is True

    def test_acquire_single_token_limit(self) -> None:
        limiter = ApprovalRateLimiter(max_per_window=1, window_seconds=60.0)
        assert limiter.acquire() is True
        assert limiter.acquire() is False


class TestAvailableTokens:
    def test_full_bucket_on_init(self) -> None:
        limiter = ApprovalRateLimiter(max_per_window=10, window_seconds=60.0)
        assert limiter.available_tokens() == 10

    def test_available_decreases_after_acquire(self) -> None:
        limiter = ApprovalRateLimiter(max_per_window=5, window_seconds=60.0)
        limiter.acquire()
        limiter.acquire()
        assert limiter.available_tokens() == 3

    def test_available_never_negative(self) -> None:
        limiter = ApprovalRateLimiter(max_per_window=2, window_seconds=60.0)
        for _ in range(10):
            limiter.acquire()
        assert limiter.available_tokens() == 0


class TestReset:
    def test_reset_refills_bucket(self) -> None:
        limiter = ApprovalRateLimiter(max_per_window=3, window_seconds=60.0)
        for _ in range(3):
            limiter.acquire()
        assert limiter.available_tokens() == 0
        limiter.reset()
        assert limiter.available_tokens() == 3

    def test_acquire_works_after_reset(self) -> None:
        limiter = ApprovalRateLimiter(max_per_window=1, window_seconds=60.0)
        limiter.acquire()
        assert limiter.acquire() is False
        limiter.reset()
        assert limiter.acquire() is True


class TestValidation:
    def test_zero_max_raises(self) -> None:
        with pytest.raises(ValueError, match="max_per_window"):
            ApprovalRateLimiter(max_per_window=0)

    def test_negative_window_raises(self) -> None:
        with pytest.raises(ValueError, match="window_seconds"):
            ApprovalRateLimiter(window_seconds=-1.0)


class TestThreadSafety:
    def test_concurrent_acquire_respects_limit(self) -> None:
        max_tokens = 10
        limiter = ApprovalRateLimiter(max_per_window=max_tokens, window_seconds=60.0)
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            result = limiter.acquire()
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly max_tokens successes, the rest denied
        successes = sum(1 for r in results if r)
        assert successes == max_tokens


# ---------------------------------------------------------------------------
# Adversarial: concurrent acquire() + reset() race (Gap #9)
# ---------------------------------------------------------------------------


class TestAdversarialRateLimiter:
    """Adversarial tests for ApprovalRateLimiter -- attacker mindset.

    Focus: concurrent acquire() + reset() race condition.
    Goal: no crashes, counter never negative, limiter always in valid state.
    """

    def test_concurrent_acquire_and_reset_no_crash(self) -> None:
        """10 acquirers + 2 resetters simultaneously must not crash.

        Race: reset() clears timestamps while acquire() reads/writes them.
        Both operations hold the lock, so no data race should occur.
        """
        limiter = ApprovalRateLimiter(max_per_window=5, window_seconds=60.0)
        errors: list[Exception] = []
        barrier = threading.Barrier(12)  # 10 acquirers + 2 resetters

        def acquirer() -> None:
            barrier.wait()
            try:
                limiter.acquire()
            except Exception as exc:
                errors.append(exc)

        def resetter() -> None:
            barrier.wait()
            try:
                limiter.reset()
            except Exception as exc:
                errors.append(exc)

        threads = (
            [threading.Thread(target=acquirer) for _ in range(10)]
            + [threading.Thread(target=resetter) for _ in range(2)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(errors) == 0, f"Unexpected errors: {errors}"

    def test_counter_never_negative_after_concurrent_reset(self) -> None:
        """available_tokens() must never return a negative value after
        concurrent acquire() + reset() calls.

        The sliding-window implementation uses a timestamp list; reset()
        clears it entirely.  Concurrent access must not leave the list in
        a state where len(timestamps) > max (which would make available=0,
        not negative, but we also confirm it stays >= 0).
        """
        limiter = ApprovalRateLimiter(max_per_window=3, window_seconds=60.0)
        errors: list[Exception] = []
        available_readings: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(20):
                try:
                    limiter.acquire()
                    tokens = limiter.available_tokens()
                    with lock:
                        available_readings.append(tokens)
                except Exception as exc:
                    errors.append(exc)

        def resetter() -> None:
            for _ in range(10):
                try:
                    limiter.reset()
                    time.sleep(0.001)
                except Exception as exc:
                    errors.append(exc)

        threads = (
            [threading.Thread(target=worker) for _ in range(5)]
            + [threading.Thread(target=resetter) for _ in range(2)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(errors) == 0, f"Unexpected errors: {errors}"
        # Every reading must be non-negative
        assert all(v >= 0 for v in available_readings), (
            f"Negative available_tokens detected: {[v for v in available_readings if v < 0]}"
        )
        # After all threads finish, limiter must be in a valid state
        final_available = limiter.available_tokens()
        assert 0 <= final_available <= 3
