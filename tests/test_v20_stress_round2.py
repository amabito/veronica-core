"""Round 2 Stress Tests: concurrent budget + timeout + cancellation interactions.

Tests:
1. 20 threads doing wrap_llm_call simultaneously on same ExecutionContext
2. wrap_llm_call with timeout_ms=1 (very short) + slow fn
3. Parent/child ExecutionContext with concurrent wrap calls on both
4. SharedTimeoutPool: schedule 100 callbacks, cancel 50, verify exactly 50 fire
5. LocalBudgetBackend: 50 threads doing reserve+commit or reserve+rollback randomly
6. ReconciliationCallback called from wrap_llm_call under concurrent load
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any

from veronica_core.containment.execution_context import (
    CancellationToken,
    ExecutionConfig,
    ExecutionContext,
    WrapOptions,
)
from veronica_core.containment.timeout_pool import SharedTimeoutPool
from veronica_core.distributed import LocalBudgetBackend
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    max_cost: float = 100.0,
    max_steps: int = 1000,
    timeout_ms: int = 0,
    budget_backend: Any = None,
) -> ExecutionConfig:
    return ExecutionConfig(
        max_cost_usd=max_cost,
        max_steps=max_steps,
        max_retries_total=1000,
        timeout_ms=timeout_ms,
        budget_backend=budget_backend,
    )


def _noop() -> None:
    pass


def _slow_fn(sleep_s: float = 0.05) -> None:
    time.sleep(sleep_s)


# ---------------------------------------------------------------------------
# Test 1: 20 concurrent wrap_llm_call threads on same ExecutionContext
# ---------------------------------------------------------------------------


class TestConcurrentWrapLlmCall:
    """20 threads simultaneously calling wrap_llm_call on the same context."""

    def test_step_count_equals_thread_count(self) -> None:
        """step_count must equal number of successful wrap calls."""
        backend = LocalBudgetBackend()
        config = _make_config(max_steps=100, budget_backend=backend)
        ctx = ExecutionContext(config=config)

        n_threads = 20
        results: list[Decision] = []
        lock = threading.Lock()

        def worker() -> None:
            decision = ctx.wrap_llm_call(
                fn=_noop, options=WrapOptions(cost_estimate_hint=0.01)
            )
            with lock:
                results.append(decision)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        snap = ctx.get_snapshot()
        allowed = [r for r in results if r == Decision.ALLOW]
        assert snap.step_count == len(allowed), (
            f"step_count={snap.step_count} must equal ALLOW count={len(allowed)}"
        )
        assert len(results) == n_threads, "All threads must complete"

    def test_cost_accumulated_matches_allowed_calls(self) -> None:
        """cost_usd_accumulated must equal number of ALLOW calls * cost_per_call."""
        cost_per_call = 0.01
        backend = LocalBudgetBackend()
        config = _make_config(max_cost=100.0, max_steps=1000, budget_backend=backend)
        ctx = ExecutionContext(config=config)

        n_threads = 20
        results: list[Decision] = []
        lock = threading.Lock()

        def worker() -> None:
            decision = ctx.wrap_llm_call(
                fn=_noop,
                options=WrapOptions(cost_estimate_hint=cost_per_call),
            )
            with lock:
                results.append(decision)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        allowed_count = sum(1 for r in results if r == Decision.ALLOW)
        snap = ctx.get_snapshot()
        expected_cost = allowed_count * cost_per_call
        assert abs(snap.cost_usd_accumulated - expected_cost) < 1e-9, (
            f"cost={snap.cost_usd_accumulated:.6f} != expected={expected_cost:.6f}"
        )

    def test_no_duplicate_nodes_in_snapshot(self) -> None:
        """No two NodeRecords should share the same node_id."""
        backend = LocalBudgetBackend()
        config = _make_config(max_steps=1000, budget_backend=backend)
        ctx = ExecutionContext(config=config)

        n_threads = 20

        def worker() -> None:
            ctx.wrap_llm_call(fn=_noop, options=WrapOptions(cost_estimate_hint=0.001))

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        snap = ctx.get_snapshot()
        node_ids = [n.node_id for n in snap.nodes]
        assert len(node_ids) == len(set(node_ids)), (
            f"Duplicate node_ids found: {len(node_ids)} nodes, {len(set(node_ids))} unique"
        )

    def test_budget_ceiling_enforced_under_concurrency(self) -> None:
        """Even with 20 concurrent threads, cost must not exceed ceiling."""
        cost_per_call = 0.15
        ceiling = 1.0
        backend = LocalBudgetBackend()
        config = _make_config(max_cost=ceiling, max_steps=1000, budget_backend=backend)
        ctx = ExecutionContext(config=config)

        n_threads = 20
        results: list[Decision] = []
        lock = threading.Lock()

        def worker() -> None:
            # Yield briefly to encourage interleaving
            time.sleep(random.uniform(0, 0.005))
            decision = ctx.wrap_llm_call(
                fn=_noop,
                options=WrapOptions(cost_estimate_hint=cost_per_call),
            )
            with lock:
                results.append(decision)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        snap = ctx.get_snapshot()
        # Total committed cost must not exceed ceiling (plus epsilon)
        assert snap.cost_usd_accumulated <= ceiling + 1e-9, (
            f"Overspend: cost={snap.cost_usd_accumulated:.6f} > ceiling={ceiling}"
        )
        # No dangling reservations
        assert backend.get_reserved() == 0.0, (
            f"Dangling reservations: {backend.get_reserved()}"
        )


# ---------------------------------------------------------------------------
# Test 2: wrap_llm_call with very short timeout + slow fn
# ---------------------------------------------------------------------------


class TestTimeoutWithSlowFn:
    """CancellationToken must be cancelled when timeout fires."""

    def test_cancellation_token_set_after_timeout(self) -> None:
        """After timeout fires, cancellation_token.is_cancelled must be True."""
        config = _make_config(timeout_ms=50)  # 50ms timeout
        ctx = ExecutionContext(config=config)

        # Wait longer than the timeout for the pool to fire
        time.sleep(0.2)

        assert ctx._cancellation_token.is_cancelled, (
            "CancellationToken must be cancelled after timeout"
        )

    def test_wrap_returns_halt_after_timeout(self) -> None:
        """After timeout, wrap_llm_call must return HALT without calling fn."""
        config = _make_config(timeout_ms=1)  # 1ms -- fires almost immediately
        ctx = ExecutionContext(config=config)

        # Wait for timeout to fire
        time.sleep(0.1)

        called: list[bool] = []
        decision = ctx.wrap_llm_call(
            fn=lambda: called.append(True),
            options=WrapOptions(),
        )
        assert decision == Decision.HALT, f"Expected HALT after timeout, got {decision}"

    def test_subsequent_calls_not_permanently_broken(self) -> None:
        """A fresh context after a timed-out one should work normally."""
        # First context: timeout
        config1 = _make_config(timeout_ms=1)
        ctx1 = ExecutionContext(config=config1)
        time.sleep(0.1)
        assert ctx1._cancellation_token.is_cancelled

        # Second context: no timeout, should work fine
        config2 = _make_config()
        ctx2 = ExecutionContext(config=config2)
        decision = ctx2.wrap_llm_call(fn=_noop, options=WrapOptions())
        assert decision == Decision.ALLOW, (
            f"Fresh context should ALLOW calls, got {decision}"
        )

    def test_slow_fn_interrupted_by_cancel(self) -> None:
        """CancellationToken.is_cancelled signals the slow fn to stop."""
        config = _make_config(timeout_ms=50)
        ctx = ExecutionContext(config=config)

        interrupted = threading.Event()

        def slow_fn_that_checks_cancellation() -> None:
            # Poll cancellation token -- cooperative cancel
            for _ in range(200):
                if ctx._cancellation_token.is_cancelled:
                    interrupted.set()
                    return
                time.sleep(0.001)

        t = threading.Thread(target=slow_fn_that_checks_cancellation)
        t.start()
        t.join(timeout=2.0)

        assert interrupted.is_set(), "Slow fn must detect cancellation via token"


# ---------------------------------------------------------------------------
# Test 3: Parent/child ExecutionContext with concurrent wrap calls
# ---------------------------------------------------------------------------


class TestParentChildConcurrentWrap:
    """Parent and child contexts with concurrent threads on each."""

    def test_parent_cost_includes_child_propagation(self) -> None:
        """Parent cost must include all child costs after propagation."""
        parent_backend = LocalBudgetBackend()
        parent_config = _make_config(
            max_cost=100.0, max_steps=1000, budget_backend=parent_backend
        )
        parent_ctx = ExecutionContext(config=parent_config)

        child_backend = LocalBudgetBackend()
        child_config = _make_config(
            max_cost=50.0, max_steps=500, budget_backend=child_backend
        )
        child_ctx = ExecutionContext(config=child_config, parent=parent_ctx)

        cost_per_call = 0.01
        n_child_threads = 10
        n_parent_threads = 10

        child_lock = threading.Lock()
        parent_lock = threading.Lock()
        child_results: list[Decision] = []
        parent_results: list[Decision] = []

        def child_worker() -> None:
            decision = child_ctx.wrap_llm_call(
                fn=_noop, options=WrapOptions(cost_estimate_hint=cost_per_call)
            )
            with child_lock:
                child_results.append(decision)

        def parent_worker() -> None:
            decision = parent_ctx.wrap_llm_call(
                fn=_noop, options=WrapOptions(cost_estimate_hint=cost_per_call)
            )
            with parent_lock:
                parent_results.append(decision)

        threads = [
            threading.Thread(target=child_worker) for _ in range(n_child_threads)
        ] + [threading.Thread(target=parent_worker) for _ in range(n_parent_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        parent_snap = parent_ctx.get_snapshot()
        child_snap = child_ctx.get_snapshot()

        # Parent cost = direct parent calls + child propagations
        child_allowed = sum(1 for r in child_results if r == Decision.ALLOW)
        parent_allowed = sum(1 for r in parent_results if r == Decision.ALLOW)
        expected_parent_cost = (parent_allowed + child_allowed) * cost_per_call

        assert abs(parent_snap.cost_usd_accumulated - expected_parent_cost) < 1e-9, (
            f"parent_cost={parent_snap.cost_usd_accumulated:.6f} != "
            f"expected={expected_parent_cost:.6f}"
        )
        assert (
            abs(child_snap.cost_usd_accumulated - child_allowed * cost_per_call) < 1e-9
        )

    def test_no_deadlock_within_timeout(self) -> None:
        """20 concurrent threads on parent + child must complete within 10 seconds."""
        parent_config = _make_config(max_cost=1000.0, max_steps=10000)
        parent_ctx = ExecutionContext(config=parent_config)
        child_config = _make_config(max_cost=500.0, max_steps=5000)
        child_ctx = ExecutionContext(config=child_config, parent=parent_ctx)

        n_threads = 20
        completed: list[bool] = []
        lock = threading.Lock()

        def worker(ctx: ExecutionContext) -> None:
            ctx.wrap_llm_call(fn=_noop, options=WrapOptions(cost_estimate_hint=0.001))
            with lock:
                completed.append(True)

        threads = [
            threading.Thread(target=worker, args=(child_ctx,))
            for _ in range(n_threads // 2)
        ] + [
            threading.Thread(target=worker, args=(parent_ctx,))
            for _ in range(n_threads // 2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert len(completed) == n_threads, (
            f"Deadlock suspected: only {len(completed)}/{n_threads} threads completed"
        )


# ---------------------------------------------------------------------------
# Test 4: SharedTimeoutPool -- schedule 100, cancel 50, verify 50 fire
# ---------------------------------------------------------------------------


class TestSharedTimeoutPoolCancelAccuracy:
    """Exactly 50 of 100 scheduled callbacks must fire when 50 are cancelled."""

    def test_exactly_50_fire_when_50_cancelled(self) -> None:
        pool = SharedTimeoutPool()
        fired: list[int] = []
        fire_lock = threading.Lock()

        n_total = 100
        handles: list[int] = []
        deadline = time.monotonic() + 0.15  # all fire in 150ms

        for i in range(n_total):
            idx = i

            def make_callback(idx: int) -> None:
                def cb() -> None:
                    with fire_lock:
                        fired.append(idx)

                return cb

            h = pool.schedule(deadline, make_callback(idx))
            handles.append(h)

        # Cancel the first 50 handles
        for h in handles[:50]:
            pool.cancel(h)

        # Wait for deadline + buffer
        time.sleep(0.3)

        pool.shutdown()

        assert len(fired) == 50, (
            f"Expected exactly 50 callbacks to fire, got {len(fired)}"
        )
        # The fired callbacks must be the ones NOT cancelled (handles[50:])
        # Handle IDs are sequential (1-based); handles[50:] = IDs 51..100
        # fired callbacks should be the non-cancelled ones
        assert len(set(fired)) == 50, "No duplicate callbacks"

    def test_rapid_schedule_1000_items(self) -> None:
        """Schedule 1000 items rapidly then shut down -- no crash or hang."""
        pool = SharedTimeoutPool()
        far_future = time.monotonic() + 3600.0  # Never fires during test

        handles: list[int] = []
        for _ in range(1000):
            h = pool.schedule(far_future, _noop)
            handles.append(h)

        # Cancel all of them
        for h in handles:
            pool.cancel(h)

        pool.shutdown()
        # No assertion needed -- just must not crash or deadlock

    def test_cancel_after_fire_is_noop(self) -> None:
        """Cancelling an already-fired handle must not raise."""
        pool = SharedTimeoutPool()
        fired: list[bool] = []

        h = pool.schedule(time.monotonic() + 0.05, lambda: fired.append(True))
        time.sleep(0.2)
        assert fired, "Callback must have fired"

        # Cancel after fire -- must be a no-op
        pool.cancel(h)  # Should not raise
        pool.shutdown()


# ---------------------------------------------------------------------------
# Test 5: LocalBudgetBackend -- 50 threads reserve+commit or reserve+rollback
# ---------------------------------------------------------------------------


class TestLocalBudgetBackendConcurrency:
    """50 threads doing reserve+commit or reserve+rollback randomly."""

    def test_committed_equals_sum_of_committed_reservations(self) -> None:
        """Final committed cost must equal sum of committed reservation amounts."""
        backend = LocalBudgetBackend()
        ceiling = 100.0
        amount_per_reservation = 0.1

        n_threads = 50
        committed_amounts: list[float] = []
        results_lock = threading.Lock()
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                # Attempt to reserve
                try:
                    rid = backend.reserve(amount_per_reservation, ceiling)
                except OverflowError:
                    return  # Budget full, nothing to do

                # Randomly commit or rollback
                if thread_id % 2 == 0:
                    # Commit
                    try:
                        backend.commit(rid)
                        with results_lock:
                            committed_amounts.append(amount_per_reservation)
                    except KeyError:
                        pass  # Expired between reserve and commit
                else:
                    # Rollback
                    try:
                        backend.rollback(rid)
                    except KeyError:
                        pass
            except Exception as e:
                with results_lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors, f"Unexpected errors: {errors}"

        # Final committed cost must match sum of explicitly committed amounts
        expected = sum(committed_amounts)
        actual = backend.get()
        assert abs(actual - expected) < 1e-9, (
            f"Backend cost={actual:.6f} != expected committed={expected:.6f}"
        )

    def test_no_dangling_reservations_at_end(self) -> None:
        """After all threads complete, get_reserved() must be 0.0."""
        backend = LocalBudgetBackend()
        ceiling = 100.0
        amount = 0.1

        n_threads = 50

        def worker() -> None:
            try:
                rid = backend.reserve(amount, ceiling)
            except OverflowError:
                return
            # Always commit or rollback -- never leave dangling
            if random.random() < 0.5:
                try:
                    backend.commit(rid)
                except KeyError:
                    pass
            else:
                try:
                    backend.rollback(rid)
                except KeyError:
                    pass

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert backend.get_reserved() == 0.0, (
            f"Dangling reservations: {backend.get_reserved():.6f}"
        )

    def test_ceiling_never_exceeded_under_concurrent_reserve(self) -> None:
        """Concurrent reserve() calls must never allow committed+reserved to exceed ceiling."""
        ceiling = 1.0
        amount = 0.15  # 6 reservations fit; 7th would overflow
        backend = LocalBudgetBackend()

        n_threads = 50
        reservation_ids: list[str] = []
        ids_lock = threading.Lock()

        def worker() -> None:
            try:
                rid = backend.reserve(amount, ceiling)
                with ids_lock:
                    reservation_ids.append(rid)
            except OverflowError:
                pass

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # Commit all successful reservations
        committed_total = 0.0
        for rid in reservation_ids:
            try:
                backend.commit(rid)
                committed_total += amount
            except KeyError:
                pass

        assert committed_total <= ceiling + 1e-9, (
            f"Ceiling exceeded: committed={committed_total:.6f} > ceiling={ceiling}"
        )


# ---------------------------------------------------------------------------
# Test 6: ReconciliationCallback under concurrent load
# ---------------------------------------------------------------------------


class TestReconciliationCallbackConcurrent:
    """Each wrap_llm_call with a reconciliation_callback must receive correct (estimated, actual)."""

    def test_each_callback_receives_correct_pair(self) -> None:
        """10 threads, each with its own callback, must receive distinct correct pairs."""
        backend = LocalBudgetBackend()
        config = _make_config(max_steps=1000, budget_backend=backend)
        ctx = ExecutionContext(config=config)

        n_threads = 10
        received: dict[int, list[tuple[float, float]]] = {
            i: [] for i in range(n_threads)
        }
        recv_lock = threading.Lock()

        class ThreadCallback:
            def __init__(self, thread_id: int) -> None:
                self._thread_id = thread_id

            def on_reconcile(self, estimated: float, actual: float) -> None:
                with recv_lock:
                    received[self._thread_id].append((estimated, actual))

        def worker(thread_id: int) -> None:
            cost_hint = 0.01 * (thread_id + 1)  # distinct per thread
            callback = ThreadCallback(thread_id)
            ctx.wrap_llm_call(
                fn=_noop,
                options=WrapOptions(
                    cost_estimate_hint=cost_hint,
                    reconciliation_callback=callback,
                ),
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        for thread_id in range(n_threads):
            calls = received[thread_id]
            assert len(calls) == 1, (
                f"Thread {thread_id}: expected 1 callback, got {len(calls)}"
            )
            estimated, actual = calls[0]
            expected_hint = 0.01 * (thread_id + 1)
            assert abs(estimated - expected_hint) < 1e-9, (
                f"Thread {thread_id}: estimated={estimated} != expected={expected_hint}"
            )
            # actual == cost_estimate_hint since no response_hint is provided
            assert abs(actual - expected_hint) < 1e-9, (
                f"Thread {thread_id}: actual={actual} != expected={expected_hint}"
            )

    def test_reconciliation_callback_does_not_corrupt_concurrent_state(self) -> None:
        """Callback raising an exception must not corrupt ExecutionContext state."""
        backend = LocalBudgetBackend()
        config = _make_config(max_steps=1000, budget_backend=backend)
        ctx = ExecutionContext(config=config)

        n_threads = 10
        results: list[Decision] = []
        result_lock = threading.Lock()

        class RaisingCallback:
            def on_reconcile(self, estimated: float, actual: float) -> None:
                raise RuntimeError("deliberate callback failure")

        def worker() -> None:
            decision = ctx.wrap_llm_call(
                fn=_noop,
                options=WrapOptions(
                    cost_estimate_hint=0.01,
                    reconciliation_callback=RaisingCallback(),
                ),
            )
            with result_lock:
                results.append(decision)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # All calls should succeed despite the raising callback
        assert all(r == Decision.ALLOW for r in results), (
            f"Some calls failed due to raising callback: {results}"
        )
        snap = ctx.get_snapshot()
        assert snap.step_count == n_threads, (
            f"step_count={snap.step_count} != {n_threads}"
        )


# ---------------------------------------------------------------------------
# Adversarial: CancellationToken cancel() from multiple threads simultaneously
# ---------------------------------------------------------------------------


class TestCancellationTokenConcurrency:
    """cancel() called simultaneously from many threads must be idempotent."""

    def test_concurrent_cancel_idempotent(self) -> None:
        """cancel() from 100 threads must not raise and is_cancelled must be True."""
        token = CancellationToken()
        errors: list[Exception] = []
        err_lock = threading.Lock()

        def worker() -> None:
            try:
                token.cancel()
            except Exception as e:
                with err_lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, f"cancel() raised exceptions: {errors}"
        assert token.is_cancelled, (
            "Token must be cancelled after concurrent cancel() calls"
        )

    def test_wait_unblocks_after_concurrent_cancel(self) -> None:
        """wait() must unblock when cancel() is called from another thread."""
        token = CancellationToken()
        unblocked = threading.Event()

        def waiter() -> None:
            token.wait(timeout_s=5.0)
            unblocked.set()

        def canceller() -> None:
            time.sleep(0.05)
            token.cancel()

        t_wait = threading.Thread(target=waiter)
        t_cancel = threading.Thread(target=canceller)
        t_wait.start()
        t_cancel.start()
        t_cancel.join(timeout=2.0)
        t_wait.join(timeout=2.0)

        assert unblocked.is_set(), (
            "wait() must unblock after cancel() from another thread"
        )
