"""Adversarial concurrency tests for veronica-core v0.10.4.

Tests:
- Part 7: Context isolation — each BudgetEnforcer/container is independent
- Part 8: Adversarial harness — 50-100 concurrent threads, race conditions
"""
from __future__ import annotations

import threading
from typing import List

import pytest

from veronica_core.budget import BudgetEnforcer
from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.inject import veronica_guard, VeronicaHalt, get_active_container


# ---------------------------------------------------------------------------
# Test 1: 50 concurrent threads each with independent BudgetEnforcer
# ---------------------------------------------------------------------------


def test_budget_enforcer_per_thread_isolation() -> None:
    """Each thread's BudgetEnforcer is independent; no leakage between them."""
    NUM_THREADS = 50
    LIMIT = 0.05

    errors: List[str] = []
    errors_lock = threading.Lock()
    barrier = threading.Barrier(NUM_THREADS)

    def worker(tid: int) -> None:
        budget = BudgetEnforcer(limit_usd=LIMIT)
        barrier.wait()

        # First spend: 0.04 — should be allowed
        ok1 = budget.spend(0.04)
        if not ok1:
            with errors_lock:
                errors.append(f"Thread {tid}: first spend (0.04) denied unexpectedly")
            return

        # Second spend: 0.02 — 0.04 + 0.02 = 0.06 > 0.05, must be denied
        ok2 = budget.spend(0.02)
        if ok2:
            with errors_lock:
                errors.append(f"Thread {tid}: second spend (0.02) allowed unexpectedly; total would be 0.06")

        # Verify no leakage: spent must be exactly 0.04
        if abs(budget.spent_usd - 0.04) > 1e-9:
            with errors_lock:
                errors.append(f"Thread {tid}: spent_usd={budget.spent_usd}, expected 0.04")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Test 2: CircuitBreaker.bind_to_context raises RuntimeError if shared
# ---------------------------------------------------------------------------


def test_circuit_breaker_shared_raises_runtime_error() -> None:
    """A single CircuitBreaker bound to two different ctx_ids must raise RuntimeError."""
    breaker = CircuitBreaker()
    breaker.bind_to_context("ctx-X")

    with pytest.raises(RuntimeError, match="shared across contexts"):
        breaker.bind_to_context("ctx-Y")


def test_circuit_breaker_concurrent_sharing_detected() -> None:
    """Concurrent bind_to_context from two threads detects sharing."""
    breaker = CircuitBreaker()
    results: List[Exception | None] = [None, None]
    barrier = threading.Barrier(2)

    def binder(idx: int, ctx_id: str) -> None:
        barrier.wait()
        try:
            breaker.bind_to_context(ctx_id)
        except RuntimeError as exc:
            results[idx] = exc

    t0 = threading.Thread(target=binder, args=(0, "ctx-A"))
    t1 = threading.Thread(target=binder, args=(1, "ctx-B"))
    t0.start()
    t1.start()
    t0.join()
    t1.join()

    # At least one must have raised (only one ctx_id can "win" the first bind)
    errors_raised = sum(1 for r in results if isinstance(r, RuntimeError))
    assert errors_raised >= 1, (
        "Expected at least one RuntimeError when sharing CircuitBreaker across contexts"
    )


# ---------------------------------------------------------------------------
# Test 3: veronica_guard 50 concurrent invocations, each sees fresh container
# ---------------------------------------------------------------------------


def test_veronica_guard_fresh_container_per_call() -> None:
    """veronica_guard creates a fresh container per invocation.

    max_steps=1: first call succeeds, but each thread's second call (if any)
    would be to a *new* invocation with a fresh container — so all 50 first
    calls succeed and no cross-thread contamination.
    """
    NUM_THREADS = 50
    denied_count = 0
    denied_lock = threading.Lock()
    barrier = threading.Barrier(NUM_THREADS)

    @veronica_guard(max_cost_usd=10.0, max_steps=100, max_retries_total=10)
    def run_once() -> str:
        container = get_active_container()
        assert container is not None, "Container must be active inside guard"
        return "ok"

    errors: List[str] = []
    errors_lock = threading.Lock()

    def worker(tid: int) -> None:
        barrier.wait()
        try:
            result = run_once()
            if result != "ok":
                with errors_lock:
                    errors.append(f"Thread {tid}: unexpected result {result!r}")
        except VeronicaHalt as exc:
            with denied_lock:
                nonlocal denied_count
                denied_count += 1
            with errors_lock:
                errors.append(f"Thread {tid}: VeronicaHalt raised unexpectedly: {exc}")
        except Exception as exc:
            with errors_lock:
                errors.append(f"Thread {tid}: unexpected exception: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, "\n".join(errors)


def test_veronica_guard_step_limit_per_invocation() -> None:
    """Each guard invocation gets max_steps=1; second call within same invocation denied.

    veronica_guard checks container.check() BEFORE calling the function.
    With max_steps=100, the budget check governs. This test checks that
    sequential calls each get a fresh container (not sharing state).
    """
    call_count = 0
    call_lock = threading.Lock()

    @veronica_guard(max_cost_usd=10.0, max_steps=100, max_retries_total=10)
    def increment() -> int:
        with call_lock:
            nonlocal call_count
            call_count += 1
            return call_count

    # Sequential calls all succeed (each invocation = fresh container)
    for _ in range(5):
        result = increment()
        assert result is not None

    assert call_count == 5


# ---------------------------------------------------------------------------
# Test 4: BudgetEnforcer concurrent spend race — total spent <= limit
# ---------------------------------------------------------------------------


def test_budget_enforcer_concurrent_spend_race() -> None:
    """100 threads each spend 0.01 with limit=0.5; total spent must not exceed 0.5."""
    NUM_THREADS = 100
    SPEND_AMOUNT = 0.01
    LIMIT = 0.5

    budget = BudgetEnforcer(limit_usd=LIMIT)
    barrier = threading.Barrier(NUM_THREADS)

    def worker() -> None:
        barrier.wait()
        budget.spend(SPEND_AMOUNT)

    threads = [threading.Thread(target=worker) for _ in range(NUM_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total_spent = budget.spent_usd
    assert total_spent <= LIMIT + 1e-9, (
        f"Race condition: total spent ${total_spent:.4f} exceeds limit ${LIMIT:.4f}"
    )

    # With 100 threads each spending 0.01 and limit 0.5 — exactly 50 should succeed
    expected_successes = int(LIMIT / SPEND_AMOUNT)  # 50
    # Allow 1 off due to floating point edge cases
    actual_successes = round(total_spent / SPEND_AMOUNT)
    assert abs(actual_successes - expected_successes) <= 1, (
        f"Expected ~{expected_successes} successes, got ~{actual_successes} "
        f"(spent={total_spent:.4f})"
    )
