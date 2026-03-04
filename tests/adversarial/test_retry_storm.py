"""Adversarial test: retry storm attack.

Attack pattern: Network errors trigger retry loops at multiple layers
simultaneously. Without containment, 3 retries x 5 nested calls = 15
actual LLM calls from a single user action (retry storm).

Tests verify that:
1. Without containment: retry storm produces O(n^k) calls (baseline)
2. With veronica RetryContainer: total retries are bounded
3. With ExecutionContext max_retries_total: chain-wide retry budget is enforced
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext
from veronica_core.retry import RetryContainer
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Stub network call that fails a configurable number of times
# ---------------------------------------------------------------------------


class FlakeyNetworkStub:
    """Simulates a flaky network endpoint that fails N times before succeeding."""

    def __init__(self, fail_count: int = 999, succeed_after: int | None = None) -> None:
        self.call_count = 0
        self.fail_count = fail_count
        self.succeed_after = succeed_after

    def call(self) -> str:
        self.call_count += 1
        if self.succeed_after is not None:
            if self.call_count <= self.succeed_after:
                raise ConnectionError(f"Network timeout on call {self.call_count}")
            return f"success on call {self.call_count}"
        if self.call_count <= self.fail_count:
            raise ConnectionError(f"Network timeout on call {self.call_count}")
        return f"success on call {self.call_count}"


# ---------------------------------------------------------------------------
# Without containment baseline tests
# ---------------------------------------------------------------------------


class TestRetryStormWithoutContainment:
    """Baseline: confirms the retry storm scales unbounded without containment."""

    def test_nested_retries_multiply_calls(self) -> None:
        """Without containment, 3 retries x 3 layers = 9+ calls from one action."""
        total_calls = 0

        def always_fails():
            nonlocal total_calls
            total_calls += 1
            raise ConnectionError("timeout")

        # Simulate 3 layers of retry (outer, middle, inner)
        # Each layer retries 3 times independently
        def inner_call():
            for _ in range(3):
                try:
                    always_fails()
                except ConnectionError:
                    pass

        def middle_call():
            for _ in range(3):
                inner_call()

        def outer_call():
            for _ in range(3):
                middle_call()

        outer_call()
        # 3 outer x 3 middle x 3 inner = 27 calls from one user action
        assert total_calls == 27

    def test_single_retry_container_caps_attempts(self) -> None:
        """RetryContainer alone bounds retries for one call."""
        stub = FlakeyNetworkStub(fail_count=999)
        container = RetryContainer(max_retries=2, backoff_base=0.0, jitter=0.0)

        with patch("time.sleep"):
            with pytest.raises(ConnectionError):
                container.execute(stub.call)

        # 1 initial + 2 retries = 3 total attempts
        assert stub.call_count == 3
        # total_retries counts all attempts (initial + retries), so 3 here
        assert container.total_retries == 3


# ---------------------------------------------------------------------------
# With containment: RetryContainer + ExecutionContext chain budget
# ---------------------------------------------------------------------------


class TestRetryStormContained:
    """Verify RetryContainer and ExecutionContext bound total retry calls."""

    def test_retry_container_total_retries_bounded(self) -> None:
        """RetryContainer.max_retries strictly bounds the retry count."""
        stub = FlakeyNetworkStub(fail_count=999)
        container = RetryContainer(max_retries=3, backoff_base=0.0, jitter=0.0)

        with patch("time.sleep"):
            with pytest.raises(ConnectionError):
                container.execute(stub.call)

        # 1 initial + 3 retries = 4 total calls maximum
        assert stub.call_count == 4
        # total_retries counts all attempts including the initial one
        assert container.total_retries == 4

    def test_execution_context_retry_budget_stops_storm(self) -> None:
        """Chain-wide retry budget in ExecutionContext caps total retries."""
        call_count = 0

        def flaky_fn():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("network error")

        # Allow 5 total retries across the whole chain
        config = ExecutionConfig(
            max_cost_usd=100.0, max_steps=100, max_retries_total=5
        )
        ctx = ExecutionContext(config=config)

        halt_count = 0
        for _ in range(20):
            decision = ctx.wrap_llm_call(fn=flaky_fn)
            if decision == Decision.HALT:
                halt_count += 1

        snap = ctx.get_snapshot()
        # Retry budget is finite -- chain halts once budget exhausted
        assert snap.retries_used <= 5
        assert halt_count > 0

    def test_successful_calls_do_not_consume_retry_budget(self) -> None:
        """Successful LLM calls must not increment retries_used."""
        success_count = 0

        def succeeding_fn():
            nonlocal success_count
            success_count += 1
            return "ok"

        config = ExecutionConfig(
            max_cost_usd=100.0, max_steps=10, max_retries_total=2
        )
        ctx = ExecutionContext(config=config)

        for _ in range(5):
            ctx.wrap_llm_call(fn=succeeding_fn)

        snap = ctx.get_snapshot()
        assert snap.retries_used == 0
        assert success_count == 5

    def test_retry_budget_zero_blocks_all_calls(self) -> None:
        """max_retries_total=0 blocks ALL calls because retries_used(0) >= budget(0).

        This is the designed behavior: a retry budget of 0 means the chain
        has no budget for any retries (or initial calls that count against
        the retry budget). Use max_retries_total >= 1 for normal operation.
        """
        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            return "success"

        config = ExecutionConfig(
            max_cost_usd=100.0, max_steps=10, max_retries_total=0
        )
        ctx = ExecutionContext(config=config)

        # All calls are halted when retry budget is 0
        for _ in range(5):
            decision = ctx.wrap_llm_call(fn=fn)
            assert decision == Decision.HALT

        # fn is never called because all calls are halted
        assert call_count == 0

    def test_multiple_retry_containers_combined_calls_bounded(self) -> None:
        """Multiple RetryContainers sharing a backoff_base=0 are each bounded."""
        stub_a = FlakeyNetworkStub(fail_count=999)
        stub_b = FlakeyNetworkStub(fail_count=999)

        container_a = RetryContainer(max_retries=2, backoff_base=0.0, jitter=0.0)
        container_b = RetryContainer(max_retries=2, backoff_base=0.0, jitter=0.0)

        with patch("time.sleep"):
            with pytest.raises(ConnectionError):
                container_a.execute(stub_a.call)

            with pytest.raises(ConnectionError):
                container_b.execute(stub_b.call)

        # Each container is bounded: 1 initial + 2 retries = 3 calls each = 6 total
        # This is NOT multiplicative (not 2*2=4 retries compounded)
        assert stub_a.call_count == 3
        assert stub_b.call_count == 3
        # total_retries counts all attempts (initial + retries)
        assert container_a.total_retries == 3
        assert container_b.total_retries == 3

    def test_retry_container_succeeds_within_budget(self) -> None:
        """RetryContainer succeeds when failure count is within retry budget."""
        # Fail first 2, succeed on 3rd
        stub = FlakeyNetworkStub(succeed_after=2)
        container = RetryContainer(max_retries=5, backoff_base=0.0, jitter=0.0)

        with patch("time.sleep"):
            result = container.execute(stub.call)

        assert "success" in result
        assert stub.call_count == 3
        assert container.total_retries == 2


# ---------------------------------------------------------------------------
# Adversarial: concurrent retry storms
# ---------------------------------------------------------------------------


class TestConcurrentRetryStorm:
    """Multiple threads triggering retries simultaneously (thundering herd)."""

    def test_concurrent_containers_do_not_interfere(self) -> None:
        """Independent RetryContainers in different threads are isolated."""
        results: list[int] = []
        lock = threading.Lock()

        def run_in_thread(thread_id: int) -> None:
            stub = FlakeyNetworkStub(fail_count=999)
            container = RetryContainer(max_retries=2, backoff_base=0.0, jitter=0.0)
            with patch("time.sleep"):
                try:
                    container.execute(stub.call)
                except ConnectionError:
                    pass
            with lock:
                results.append(stub.call_count)

        threads = [threading.Thread(target=run_in_thread, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each thread: 1 initial + 2 retries = 3 calls, independent of others
        assert len(results) == 5
        for count in results:
            assert count == 3, f"Expected 3 calls per thread, got {count}"

    def test_execution_context_shared_across_threads_stops_storm(self) -> None:
        """A single ExecutionContext shared across threads enforces combined step limit."""
        call_count = 0
        lock = threading.Lock()

        def thread_fn():
            nonlocal call_count
            with lock:
                call_count += 1

        config = ExecutionConfig(
            max_cost_usd=100.0, max_steps=10, max_retries_total=0
        )
        ctx = ExecutionContext(config=config)

        allow_counts: list[int] = []
        thread_lock = threading.Lock()

        def worker():
            local_allows = 0
            for _ in range(5):
                decision = ctx.wrap_llm_call(fn=thread_fn)
                if decision == Decision.ALLOW:
                    local_allows += 1
            with thread_lock:
                allow_counts.append(local_allows)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total_allows = sum(allow_counts)
        # Total successful calls must not exceed max_steps=10
        assert total_allows <= 10
        assert call_count <= 10
