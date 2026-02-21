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
