"""Tests for _LimitChecker -- Phase 2 ExecutionContext decomposition.

Covers:
- Step counting: increment, check against max
- Cost accumulation: add, check against ceiling
- Retry counting: increment, check against budget
- Abort: mark_aborted, idempotent
- elapsed_ms: correct monotonic time calculation
- check_limits() priority order: aborted > budget > steps > retries > timeout
- Thread safety: 10 threads incrementing step/cost simultaneously
- Zero config edge cases: max_cost=0, max_steps=0
"""

from __future__ import annotations

import threading
import time


from veronica_core.containment._limit_checker import _LimitChecker
from veronica_core.containment.types import CancellationToken, ExecutionConfig
from veronica_core.distributed import LocalBudgetBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_checker(
    max_cost_usd: float = 10.0,
    max_steps: int = 50,
    max_retries_total: int = 10,
    timeout_ms: int = 0,
    cancelled: bool = False,
) -> _LimitChecker:
    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps,
        max_retries_total=max_retries_total,
        timeout_ms=timeout_ms,
    )
    token = CancellationToken()
    if cancelled:
        token.cancel()
    return _LimitChecker(config=config, cancellation_token=token)


def _noop_emit(stop_reason: str, detail: str) -> None:
    """No-op emit function for check_limits calls that don't need to capture events."""


# ---------------------------------------------------------------------------
# Step counting
# ---------------------------------------------------------------------------


class TestStepCounting:
    """increment_step and step limit enforcement."""

    def test_initial_step_count_is_zero(self) -> None:
        checker = _make_checker()
        assert checker.step_count == 0

    def test_increment_step_increments_by_one(self) -> None:
        checker = _make_checker()
        checker.increment_step()
        assert checker.step_count == 1

    def test_multiple_increments_accumulate(self) -> None:
        checker = _make_checker(max_steps=100)
        for _ in range(5):
            checker.increment_step()
        assert checker.step_count == 5

    def test_step_limit_triggers_at_threshold(self) -> None:
        checker = _make_checker(max_steps=3)
        for _ in range(3):
            checker.increment_step()
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "step_limit_exceeded"

    def test_below_step_limit_returns_none(self) -> None:
        checker = _make_checker(max_steps=10)
        for _ in range(5):
            checker.increment_step()
        assert checker.check_limits(LocalBudgetBackend(), _noop_emit) is None

    def test_zero_max_steps_halts_immediately(self) -> None:
        checker = _make_checker(max_steps=0)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "step_limit_exceeded"


# ---------------------------------------------------------------------------
# Cost accumulation
# ---------------------------------------------------------------------------


class TestCostAccumulation:
    """add_cost and budget ceiling enforcement."""

    def test_initial_cost_is_zero(self) -> None:
        checker = _make_checker()
        assert checker.cost_usd_accumulated == 0.0

    def test_add_cost_accumulates(self) -> None:
        checker = _make_checker(max_cost_usd=100.0)
        checker.add_cost(0.5)
        checker.add_cost(0.25)
        assert abs(checker.cost_usd_accumulated - 0.75) < 1e-9

    def test_cost_ceiling_triggers_at_threshold(self) -> None:
        checker = _make_checker(max_cost_usd=1.0)
        checker.add_cost(1.0)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "budget_exceeded"

    def test_cost_below_ceiling_returns_none(self) -> None:
        checker = _make_checker(max_cost_usd=5.0)
        checker.add_cost(2.0)
        assert checker.check_limits(LocalBudgetBackend(), _noop_emit) is None

    def test_zero_max_cost_halts_immediately(self) -> None:
        checker = _make_checker(max_cost_usd=0.0)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "budget_exceeded"

    def test_fractional_cost_accumulates_correctly(self) -> None:
        checker = _make_checker(max_cost_usd=0.3)
        checker.add_cost(0.1)
        checker.add_cost(0.1)
        checker.add_cost(0.1)
        # sum 0.1*3 should hit the ceiling (with epsilon guard)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "budget_exceeded"


# ---------------------------------------------------------------------------
# Retry counting
# ---------------------------------------------------------------------------


class TestRetryCounting:
    """increment_retries and retry budget enforcement."""

    def test_initial_retries_is_zero(self) -> None:
        checker = _make_checker()
        assert checker.retries_used == 0

    def test_increment_retries_increments_by_one(self) -> None:
        checker = _make_checker()
        checker.increment_retries()
        assert checker.retries_used == 1

    def test_retry_budget_triggers_at_threshold(self) -> None:
        checker = _make_checker(max_retries_total=3)
        for _ in range(3):
            checker.increment_retries()
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "retry_budget_exceeded"

    def test_below_retry_budget_returns_none(self) -> None:
        checker = _make_checker(max_retries_total=5)
        for _ in range(2):
            checker.increment_retries()
        assert checker.check_limits(LocalBudgetBackend(), _noop_emit) is None


# ---------------------------------------------------------------------------
# Abort
# ---------------------------------------------------------------------------


class TestAbort:
    """mark_aborted idempotence and is_aborted property."""

    def test_not_aborted_initially(self) -> None:
        checker = _make_checker()
        assert not checker.is_aborted

    def test_mark_aborted_returns_true_on_first_call(self) -> None:
        checker = _make_checker()
        result = checker.mark_aborted("test_reason")
        assert result is True

    def test_mark_aborted_returns_false_on_second_call(self) -> None:
        checker = _make_checker()
        checker.mark_aborted("first")
        result = checker.mark_aborted("second")
        assert result is False

    def test_abort_reason_set_on_first_call(self) -> None:
        checker = _make_checker()
        checker.mark_aborted("reason_one")
        assert checker.abort_reason == "reason_one"

    def test_abort_reason_unchanged_after_second_call(self) -> None:
        checker = _make_checker()
        checker.mark_aborted("first_reason")
        checker.mark_aborted("second_reason")
        assert checker.abort_reason == "first_reason"

    def test_is_aborted_true_after_mark(self) -> None:
        checker = _make_checker()
        checker.mark_aborted("some_reason")
        assert checker.is_aborted

    def test_aborted_context_returns_aborted_from_check_limits(self) -> None:
        checker = _make_checker()
        checker.mark_aborted("manual_abort")
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "aborted"

    def test_mark_closed_sets_context_closed_reason(self) -> None:
        checker = _make_checker()
        result = checker.mark_closed()
        assert result is True
        assert checker.abort_reason == "context_closed"

    def test_mark_closed_idempotent(self) -> None:
        checker = _make_checker()
        checker.mark_closed()
        result = checker.mark_closed()
        assert result is False


# ---------------------------------------------------------------------------
# elapsed_ms
# ---------------------------------------------------------------------------


class TestElapsedMs:
    """elapsed_ms reflects monotonic time since construction."""

    def test_elapsed_ms_is_nonnegative(self) -> None:
        checker = _make_checker()
        assert checker.elapsed_ms >= 0.0

    def test_elapsed_ms_increases_with_time(self) -> None:
        checker = _make_checker()
        t0 = checker.elapsed_ms
        time.sleep(0.05)
        t1 = checker.elapsed_ms
        assert t1 > t0

    def test_elapsed_ms_approximate_sleep(self) -> None:
        checker = _make_checker()
        time.sleep(0.1)
        elapsed = checker.elapsed_ms
        # Should be at least 80ms, allowing scheduler jitter
        assert elapsed >= 80.0


# ---------------------------------------------------------------------------
# check_limits() priority ordering
# ---------------------------------------------------------------------------


class TestCheckLimitsPriority:
    """Priority: aborted > budget > steps > retries > timeout."""

    def test_aborted_beats_budget_exceeded(self) -> None:
        checker = _make_checker(max_cost_usd=0.0)
        checker.mark_aborted("manual")
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "aborted"

    def test_aborted_beats_step_limit(self) -> None:
        checker = _make_checker(max_steps=0)
        checker.mark_aborted("manual")
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "aborted"

    def test_budget_beats_step_limit(self) -> None:
        checker = _make_checker(max_cost_usd=0.0, max_steps=0)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "budget_exceeded"

    def test_step_limit_beats_retry_budget(self) -> None:
        checker = _make_checker(max_cost_usd=100.0, max_steps=0, max_retries_total=0)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "step_limit_exceeded"

    def test_retry_budget_exceeded_returned_when_others_clear(self) -> None:
        checker = _make_checker(max_cost_usd=100.0, max_steps=100, max_retries_total=0)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "retry_budget_exceeded"

    def test_timeout_returned_when_cancelled(self) -> None:
        checker = _make_checker(
            max_cost_usd=100.0, max_steps=100, max_retries_total=100, cancelled=True
        )
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "timeout"

    def test_none_when_all_within_limits(self) -> None:
        checker = _make_checker()
        assert checker.check_limits(LocalBudgetBackend(), _noop_emit) is None

    def test_emit_fn_called_for_step_limit(self) -> None:
        checker = _make_checker(max_steps=0)
        emitted: list[tuple[str, str]] = []
        checker.check_limits(LocalBudgetBackend(), lambda r, d: emitted.append((r, d)))
        assert any(r == "step_limit_exceeded" for r, _ in emitted)

    def test_emit_fn_called_for_budget_exceeded(self) -> None:
        checker = _make_checker(max_cost_usd=0.0)
        emitted: list[tuple[str, str]] = []
        checker.check_limits(LocalBudgetBackend(), lambda r, d: emitted.append((r, d)))
        assert any(r == "budget_exceeded" for r, _ in emitted)


# ---------------------------------------------------------------------------
# snapshot_counters
# ---------------------------------------------------------------------------


class TestSnapshotCounters:
    """snapshot_counters returns consistent dict under concurrent access."""

    def test_snapshot_contains_all_keys(self) -> None:
        checker = _make_checker()
        snap = checker.snapshot_counters()
        assert "step_count" in snap
        assert "cost_usd_accumulated" in snap
        assert "retries_used" in snap
        assert "aborted" in snap
        assert "abort_reason" in snap
        assert "elapsed_ms" in snap

    def test_snapshot_reflects_mutations(self) -> None:
        checker = _make_checker()
        checker.increment_step()
        checker.add_cost(1.5)
        checker.increment_retries()
        snap = checker.snapshot_counters()
        assert snap["step_count"] == 1
        assert abs(snap["cost_usd_accumulated"] - 1.5) < 1e-9
        assert snap["retries_used"] == 1
        assert snap["aborted"] is False


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """10 threads incrementing step/cost simultaneously -- no data corruption."""

    def test_concurrent_increment_step(self) -> None:
        checker = _make_checker(max_steps=10_000)
        n_threads = 10
        increments_per_thread = 100

        def worker() -> None:
            for _ in range(increments_per_thread):
                checker.increment_step()

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert checker.step_count == n_threads * increments_per_thread

    def test_concurrent_add_cost(self) -> None:
        checker = _make_checker(max_cost_usd=1_000.0)
        n_threads = 10
        cost_per_call = 0.01
        calls_per_thread = 100

        def worker() -> None:
            for _ in range(calls_per_thread):
                checker.add_cost(cost_per_call)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected = n_threads * calls_per_thread * cost_per_call
        assert abs(checker.cost_usd_accumulated - expected) < 1e-6

    def test_concurrent_mark_aborted_exactly_one_succeeds(self) -> None:
        checker = _make_checker()
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            result = checker.mark_aborted("race_reason")
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert results.count(False) == 9
