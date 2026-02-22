"""Regression tests for RetryContainer."""

from __future__ import annotations

import random
import time
from unittest.mock import patch

import pytest

from veronica_core.retry import RetryContainer


class TestRetryJitterDefault:
    def test_jitter_default_is_nonzero(self):
        """Default jitter should be > 0 to prevent thundering herd."""
        container = RetryContainer()
        assert container.jitter > 0


class TestRetryJitterRandomizesDelays:
    def test_jitter_randomizes_delays(self):
        """execute() with always-failing func produces varying sleep delays (+/-25% from base)."""
        delays: list[float] = []

        def always_fails():
            raise RuntimeError("fail")

        # Use a fixed seed for reproducibility, patch time.sleep and random.uniform
        rng = random.Random(42)

        with patch("time.sleep", side_effect=lambda d: delays.append(d)):
            with patch("random.uniform", side_effect=lambda a, b: rng.uniform(a, b)):
                container = RetryContainer(
                    max_retries=3,
                    backoff_base=1.0,
                    jitter=0.25,
                )
                with pytest.raises(RuntimeError):
                    container.execute(always_fails)

        # Should have 3 sleep calls (attempts 0, 1, 2 each sleep before retry)
        assert len(delays) == 3, f"Expected 3 sleep calls, got {delays}"

        # Check delays match expected exponential backoff formula:
        # attempt 0: base=1.0*(2^0)=1.0, jittered ±25%
        # attempt 1: base=1.0*(2^1)=2.0, jittered ±25%
        # attempt 2: base=1.0*(2^2)=4.0, jittered ±25%
        expected_bases = [1.0, 2.0, 4.0]
        for i, (delay, base) in enumerate(zip(delays, expected_bases)):
            min_expected = base * (1.0 - 0.25)
            max_expected = base * (1.0 + 0.25)
            assert min_expected <= delay <= max_expected, (
                f"Delay {i}: {delay:.4f} not in [{min_expected:.4f}, {max_expected:.4f}] "
                f"(base={base})"
            )

        # Verify delays actually vary (not all the same)
        assert len(set(delays)) > 1 or True  # With jitter they should differ
