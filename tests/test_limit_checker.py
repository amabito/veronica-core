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


# ---------------------------------------------------------------------------
# New atomic methods (v3.2.0)
# ---------------------------------------------------------------------------


class TestAtomicMethods:
    """Tests for set_cost, set_step_count, add_cost_and_get_total, commit_success."""

    def test_set_cost_overwrites(self) -> None:
        checker = _make_checker()
        checker.add_cost(1.0)
        checker.set_cost(5.0)
        assert abs(checker.cost_usd_accumulated - 5.0) < 1e-9

    def test_set_step_count_overwrites(self) -> None:
        checker = _make_checker(max_steps=100)
        checker.increment_step()
        checker.set_step_count(42)
        assert checker.step_count == 42

    def test_add_cost_and_get_total_returns_new_total(self) -> None:
        checker = _make_checker()
        checker.add_cost(1.0)
        total = checker.add_cost_and_get_total(2.5)
        assert abs(total - 3.5) < 1e-9
        assert abs(checker.cost_usd_accumulated - 3.5) < 1e-9

    def test_add_cost_and_get_total_atomic_under_concurrency(self) -> None:
        checker = _make_checker(max_cost_usd=1_000.0)
        totals: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(100):
                t = checker.add_cost_and_get_total(0.01)
                with lock:
                    totals.append(t)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All totals should be unique (strictly increasing under lock)
        assert len(set(totals)) == 1000
        assert abs(checker.cost_usd_accumulated - 10.0) < 1e-6

    def test_commit_success_increments_both(self) -> None:
        checker = _make_checker(max_steps=100)
        checker.commit_success(0.5)
        checker.commit_success(0.3)
        assert checker.step_count == 2
        assert abs(checker.cost_usd_accumulated - 0.8) < 1e-9

    def test_commit_success_atomic_under_concurrency(self) -> None:
        checker = _make_checker(max_steps=10_000, max_cost_usd=1_000.0)

        def worker() -> None:
            for _ in range(100):
                checker.commit_success(0.01)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert checker.step_count == 1000
        assert abs(checker.cost_usd_accumulated - 10.0) < 1e-6


# ---------------------------------------------------------------------------
# Adversarial: TOCTOU / Race Conditions
# ---------------------------------------------------------------------------


class TestAdversarialLimitCheckerRace:
    """Race condition tests -- attacker mindset: exploit timing windows."""

    def test_concurrent_commit_success_no_budget_overrun(self) -> None:
        """10 threads calling commit_success() must not push cost above ceiling.

        Design: ceiling is exactly N * cost_per_commit.  After all threads
        finish, cost_usd_accumulated must equal exactly that ceiling (no
        under-count, no over-count due to lost updates).
        """
        n_threads = 10
        commits_per_thread = 50
        cost_per_commit = 0.01
        max_cost = n_threads * commits_per_thread * cost_per_commit  # 5.0

        checker = _make_checker(
            max_cost_usd=max_cost * 2,  # ceiling above total -- no halt expected
            max_steps=10_000,
        )

        def worker() -> None:
            for _ in range(commits_per_thread):
                checker.commit_success(cost_per_commit)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected = max_cost
        assert abs(checker.cost_usd_accumulated - expected) < 1e-6, (
            f"Expected {expected}, got {checker.cost_usd_accumulated} -- lost update?"
        )

    def test_concurrent_commit_success_while_check_limits_runs(self) -> None:
        """check_limits() racing with commit_success() must detect budget_exceeded
        once cost crosses the ceiling.

        Strategy: checker thread polls until all commit threads have finished,
        then a final authoritative check must return budget_exceeded.
        Also verifies at least one in-race poll caught the exceeded state.
        """
        n_threads = 10
        commits_per_thread = 50
        cost_per_commit = 0.01
        # Ceiling 0.05 -- crossed after just 5 commits across all threads.
        checker = _make_checker(max_cost_usd=0.05, max_steps=100_000)
        commits_done = threading.Event()
        check_results: list[str | None] = []
        result_lock = threading.Lock()

        def commit_worker() -> None:
            for _ in range(commits_per_thread):
                checker.commit_success(cost_per_commit)

        def check_worker() -> None:
            # Poll until commits are all done, capturing results.
            while not commits_done.is_set():
                r = checker.check_limits(LocalBudgetBackend(), _noop_emit)
                with result_lock:
                    check_results.append(r)

        check_thread = threading.Thread(target=check_worker)
        commit_threads = [threading.Thread(target=commit_worker) for _ in range(n_threads)]

        check_thread.start()
        for t in commit_threads:
            t.start()
        for t in commit_threads:
            t.join()
        commits_done.set()
        check_thread.join()

        # After all commits total = 5.0 >> ceiling 0.05.
        # The checker thread polled throughout -- must have seen budget_exceeded.
        assert "budget_exceeded" in check_results, (
            "check_limits() never detected budget_exceeded during concurrent commits"
        )
        # Final authoritative check must also be budget_exceeded.
        final = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert final == "budget_exceeded"

    def test_concurrent_mark_aborted_different_reasons_exactly_one_wins(self) -> None:
        """10 threads calling mark_aborted() with distinct reasons.

        Exactly 1 must return True; abort_reason must be one of the submitted
        strings; subsequent calls must all return False.
        """
        checker = _make_checker()
        results: list[bool] = []
        reasons_seen: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker(reason: str) -> None:
            barrier.wait()
            result = checker.mark_aborted(reason)
            with lock:
                results.append(result)
                reasons_seen.append(reason)

        threads = [
            threading.Thread(target=worker, args=(f"reason_{i}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert results.count(False) == 9
        # The winning reason must be one of the submitted reasons.
        assert checker.abort_reason is not None
        assert checker.abort_reason.startswith("reason_")

    def test_set_cost_racing_with_add_cost_and_get_total_no_corruption(self) -> None:
        """set_cost(0) racing with add_cost_and_get_total() must not lose
        increments -- both operations hold the same internal lock, so interleaving
        is serial; the final value must be non-negative and finite.
        """
        checker = _make_checker(max_cost_usd=1_000.0)
        barrier = threading.Barrier(2)

        def setter() -> None:
            barrier.wait()
            for _ in range(200):
                checker.set_cost(0.0)

        def adder() -> None:
            barrier.wait()
            for _ in range(200):
                checker.add_cost_and_get_total(0.001)

        t1 = threading.Thread(target=setter)
        t2 = threading.Thread(target=adder)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Cannot assert exact final value (set_cost resets can win last),
        # but we must not have NaN / negative / exception.
        final = checker.cost_usd_accumulated
        assert final >= 0.0
        assert final == final  # NaN check: NaN != NaN

    def test_check_limits_after_mark_closed_always_returns_aborted(self) -> None:
        """Thread A calls mark_closed(); thread B calls check_limits() in a
        tight loop.  Every result observed AFTER close completes must be
        'aborted', never None.
        """
        checker = _make_checker(max_cost_usd=1_000.0, max_steps=10_000)
        closed_event = threading.Event()
        violations: list[str] = []
        lock = threading.Lock()

        def closer() -> None:
            # Give the checker thread a moment to accumulate results.
            time.sleep(0.005)
            checker.mark_closed()
            closed_event.set()

        def checker_worker() -> None:
            closed_event.wait()
            # After close is confirmed, ALL subsequent calls must be 'aborted'.
            for _ in range(100):
                r = checker.check_limits(LocalBudgetBackend(), _noop_emit)
                if r != "aborted":
                    with lock:
                        violations.append(str(r))

        t_close = threading.Thread(target=closer)
        t_check = threading.Thread(target=checker_worker)
        t_close.start()
        t_check.start()
        t_close.join()
        t_check.join()

        assert violations == [], (
            f"check_limits() returned non-'aborted' after mark_closed(): {violations}"
        )

    def test_concurrent_increment_step_returning_final_count_matches(self) -> None:
        """100 threads each calling increment_step_returning() once.

        The set of returned values must be {1, 2, ..., 100} (no gaps, no
        duplicates) -- proving the increment-and-return is truly atomic.
        """
        n_threads = 100
        checker = _make_checker(max_steps=10_000)
        returned: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(n_threads)

        def worker() -> None:
            barrier.wait()
            v = checker.increment_step_returning()
            with lock:
                returned.append(v)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(returned) == list(range(1, n_threads + 1)), (
            "Gaps or duplicates in increment_step_returning() results -- not atomic"
        )


# ---------------------------------------------------------------------------
# Adversarial: Boundary Abuse
# ---------------------------------------------------------------------------


class TestAdversarialLimitCheckerBoundary:
    """Boundary abuse tests -- zero, negative, inf, NaN inputs."""

    def test_max_cost_zero_budget_exceeded_immediately(self) -> None:
        """max_cost_usd=0.0 must trigger budget_exceeded before any cost is added."""
        checker = _make_checker(max_cost_usd=0.0, max_steps=100, max_retries_total=100)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "budget_exceeded"

    def test_max_steps_zero_step_limit_exceeded_immediately(self) -> None:
        """max_steps=0 with cost > 0 must trigger step_limit_exceeded (budget clear)."""
        checker = _make_checker(max_cost_usd=100.0, max_steps=0, max_retries_total=100)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "step_limit_exceeded"

    def test_max_retries_zero_retry_budget_exceeded_immediately(self) -> None:
        """max_retries_total=0 (budget/steps clear) must trigger retry_budget_exceeded."""
        checker = _make_checker(max_cost_usd=100.0, max_steps=100, max_retries_total=0)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "retry_budget_exceeded"

    def test_negative_cost_via_add_cost_underflows_accumulated(self) -> None:
        """add_cost(-1.0) decrements the accumulator.

        This is a design-level concern: the production code does not guard
        against negative amounts.  Verify the behavior is at least consistent
        (no exception, deterministic value).
        """
        checker = _make_checker(max_cost_usd=10.0)
        checker.add_cost(2.0)
        checker.add_cost(-1.0)
        # Result should be 1.0 -- subtraction permitted by current design.
        assert abs(checker.cost_usd_accumulated - 1.0) < 1e-9
        # Must not trigger budget_exceeded (1.0 < 10.0).
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason is None

    def test_infinite_max_cost_rejected_by_config_guard(self) -> None:
        """ExecutionConfig must reject float('inf') as max_cost_usd.

        The guard in __post_init__ is the boundary defence preventing an
        "infinite budget" misconfiguration from silently bypassing enforcement.
        """
        import pytest

        with pytest.raises(ValueError, match="finite"):
            _make_checker(max_cost_usd=float("inf"), max_steps=10_000)

    def test_nan_max_cost_rejected_by_config_guard(self) -> None:
        """ExecutionConfig must reject float('nan') as max_cost_usd.

        NaN comparisons always return False, so without this guard
        cost + epsilon >= NaN would never fire budget_exceeded.
        """
        import pytest

        with pytest.raises(ValueError, match="finite"):
            _make_checker(max_cost_usd=float("nan"), max_steps=10_000)

    def test_nan_cost_via_add_cost_check_limits_does_not_crash(self) -> None:
        """add_cost(NaN) injects NaN into the accumulator at runtime.

        ExecutionConfig validates max_cost_usd at construction time, but it
        does NOT validate amounts passed to add_cost().  Verify that
        check_limits() at minimum does not raise an unhandled exception.

        Document current behavior: NaN + _BUDGET_EPSILON >= finite_max is
        False in CPython, so budget_exceeded may not fire.  If a runtime guard
        is added to add_cost(), this test should be updated accordingly.
        """
        checker = _make_checker(max_cost_usd=1.0, max_steps=10_000)
        checker.add_cost(float("nan"))
        try:
            reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        except Exception as exc:
            raise AssertionError(
                f"check_limits() raised {type(exc).__name__} with NaN cost: {exc}"
            ) from exc
        # Accept either outcome -- the critical property is no crash.
        assert reason in (None, "budget_exceeded"), (
            f"Unexpected reason with NaN cost: {reason}"
        )

    def test_max_steps_one_exactly_one_commit_allowed(self) -> None:
        """max_steps=1: first commit_success passes, second check_limits halts."""
        checker = _make_checker(max_cost_usd=100.0, max_steps=1)
        # Before any commit -- 0 < 1, should pass.
        assert checker.check_limits(LocalBudgetBackend(), _noop_emit) is None
        checker.commit_success(0.0)
        # After 1 commit -- step_count=1 >= max_steps=1.
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "step_limit_exceeded"


# ---------------------------------------------------------------------------
# Adversarial: State Corruption
# ---------------------------------------------------------------------------


class TestAdversarialLimitCheckerState:
    """State corruption tests -- invalid transitions, idempotence, tight loops."""

    def test_set_cost_negative_then_check_limits_no_budget_exceeded(self) -> None:
        """set_cost(-999.0) sets accumulated cost to a negative value.

        Negative cost + _BUDGET_EPSILON should be well below any positive ceiling,
        so check_limits() must return None (not budget_exceeded).
        """
        checker = _make_checker(max_cost_usd=1.0, max_steps=100)
        checker.set_cost(-999.0)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason is None, (
            f"check_limits() returned {reason!r} with cost=-999 -- unexpected"
        )

    def test_mark_aborted_then_mark_closed_idempotent(self) -> None:
        """mark_aborted() followed by mark_closed() must be idempotent.

        mark_closed() must return False (already aborted), and abort_reason
        must remain the first reason ('manual_abort'), NOT 'context_closed'.
        """
        checker = _make_checker()
        first = checker.mark_aborted("manual_abort")
        second = checker.mark_closed()

        assert first is True
        assert second is False
        assert checker.abort_reason == "manual_abort"
        assert checker.is_aborted

    def test_mark_closed_then_mark_aborted_idempotent(self) -> None:
        """mark_closed() followed by mark_aborted() must be idempotent.

        abort_reason must stay 'context_closed'.
        """
        checker = _make_checker()
        checker.mark_closed()
        result = checker.mark_aborted("late_abort")

        assert result is False
        assert checker.abort_reason == "context_closed"

    def test_rapid_commit_success_and_check_limits_loop(self) -> None:
        """1000 iterations of commit_success + check_limits in tight serial loop.

        Verifies no internal state corruption (no exception, monotonically
        increasing step_count, cost_usd_accumulated).
        """
        n = 1000
        checker = _make_checker(
            max_cost_usd=1_000_000.0,  # effectively infinite for 1000 * 0.001 = 1.0
            max_steps=n + 1,
            max_retries_total=n + 1,
        )
        prev_steps = 0
        prev_cost = 0.0

        for i in range(n):
            checker.commit_success(0.001)
            snap = checker.snapshot_counters()

            assert snap["step_count"] >= prev_steps, (
                f"step_count went backwards at iteration {i}"
            )
            assert snap["cost_usd_accumulated"] >= prev_cost - 1e-12, (
                f"cost went backwards at iteration {i}"
            )
            prev_steps = snap["step_count"]
            prev_cost = snap["cost_usd_accumulated"]

            # check_limits should remain None (limits not reached yet).
            reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
            assert reason is None, (
                f"check_limits() returned {reason!r} unexpectedly at iteration {i}"
            )

        assert checker.step_count == n
        assert abs(checker.cost_usd_accumulated - n * 0.001) < 1e-6


# ---------------------------------------------------------------------------
# Red Team: Bypass Attempts
# ---------------------------------------------------------------------------


class TestRedTeamLimitChecker:
    """Red team tests -- can an attacker bypass budget enforcement?"""

    def test_set_cost_zero_reset_cannot_bypass_budget_permanently(self) -> None:
        """Attacker calls set_cost(0.0) to reset the accumulator mid-run.

        After reset, the checker will temporarily report no budget exceeded,
        but add_cost() after reset must still accumulate correctly and
        re-trigger the ceiling when reached again.

        This documents the KNOWN RISK: set_cost() is a privileged escape hatch.
        Test ensures that at least the arithmetic remains consistent after reset.
        """
        checker = _make_checker(max_cost_usd=0.5, max_steps=10_000)
        checker.add_cost(0.4)
        # Budget not yet exceeded.
        assert checker.check_limits(LocalBudgetBackend(), _noop_emit) is None

        # Simulate attacker reset.
        checker.set_cost(0.0)
        # After reset -- cost is 0, not exceeded.
        assert checker.check_limits(LocalBudgetBackend(), _noop_emit) is None

        # Now add cost to exceed ceiling again.
        checker.add_cost(0.6)
        reason = checker.check_limits(LocalBudgetBackend(), _noop_emit)
        assert reason == "budget_exceeded", (
            "Budget enforcement failed after set_cost(0) reset -- attacker bypass possible"
        )

    def test_concurrent_set_cost_zero_cannot_suppress_budget_exceeded_permanently(
        self,
    ) -> None:
        """Attacker spawns a thread calling set_cost(0.0) in a tight loop while
        another thread calls add_cost(large_amount).

        After both threads finish, check_limits() must detect budget_exceeded
        because add_cost accumulates enough cost even if some resets win the
        race -- UNLESS set_cost(0) wins the very last write, in which case
        the cost might be 0.  The test therefore validates that the attacker
        cannot permanently suppress the limit across multiple checks during
        the race window, but accepts that the last-write-wins nature of
        set_cost() means a final check may return None.

        The KEY assertion: when cost is genuinely above ceiling, check_limits()
        reports budget_exceeded (no silent suppression within a single check).
        """
        checker = _make_checker(max_cost_usd=0.1, max_steps=10_000)
        barrier = threading.Barrier(2)
        budget_exceeded_seen = threading.Event()

        def attacker_resetter() -> None:
            barrier.wait()
            for _ in range(500):
                checker.set_cost(0.0)

        def cost_adder() -> None:
            barrier.wait()
            for _ in range(500):
                checker.add_cost(0.01)
                r = checker.check_limits(LocalBudgetBackend(), _noop_emit)
                if r == "budget_exceeded":
                    budget_exceeded_seen.set()

        t1 = threading.Thread(target=attacker_resetter)
        t2 = threading.Thread(target=cost_adder)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # At some point during the race the budget MUST have been detected.
        # (500 * 0.01 = 5.0 total added; ceiling 0.1 -- many crossings occur
        # between resets.)
        assert budget_exceeded_seen.is_set(), (
            "check_limits() never fired budget_exceeded during concurrent "
            "add_cost + set_cost(0) -- attacker may be able to suppress limits"
        )
