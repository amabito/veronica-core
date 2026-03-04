"""Adversarial tests for v2.0 budget reserve/commit/rollback + execution_context.

Attack vectors:
1. TOCTOU race: concurrent reserve() calls exceeding ceiling
2. Double commit/rollback on same reservation ID
3. Reservation expiry during commit (race window)
4. SharedTimeoutPool: callback exceptions, rapid schedule/cancel, singleton thread safety
5. HaltCategory: invalid enum values, get_halt_reason() after close()
6. close() idempotency, close() during active _wrap()
7. reconciliation_callback exception handling
8. _nesting_depth_var overflow under deep recursion
9. Redis Lua script atomicity under concurrent reserve+commit+rollback
10. Budget ceiling exactly at boundary (epsilon edge cases)
"""
from __future__ import annotations

import threading
import time

import fakeredis
import pytest

from veronica_core.containment.execution_context import (
    ExecutionConfig,
    ExecutionContext,
    WrapOptions,
)
from veronica_core.containment.timeout_pool import SharedTimeoutPool
from veronica_core.distributed import (
    LocalBudgetBackend,
    RedisBudgetBackend,
    _BUDGET_EPSILON,
    _RESERVATION_TIMEOUT_S,
)
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_local_backend() -> LocalBudgetBackend:
    return LocalBudgetBackend()


def make_redis_backend(
    fake_client: fakeredis.FakeRedis | None = None,
    chain_id: str = "test-adv",
) -> RedisBudgetBackend:
    if fake_client is None:
        fake_client = fakeredis.FakeRedis(decode_responses=True)
    backend = RedisBudgetBackend.__new__(RedisBudgetBackend)
    backend._redis_url = "redis://fake"
    backend._chain_id = chain_id
    backend._key = f"veronica:budget:{chain_id}"
    backend._ttl = 3600
    backend._fallback_on_error = True
    backend._fallback = LocalBudgetBackend()
    backend._using_fallback = False
    backend._lock = threading.Lock()
    backend._client = fake_client
    backend._fallback_seed_base = 0.0
    return backend


def make_ctx(
    max_cost: float = 1.0,
    max_steps: int = 100,
    budget_backend: LocalBudgetBackend | None = None,
) -> ExecutionContext:
    cfg = ExecutionConfig(
        max_cost_usd=max_cost,
        max_steps=max_steps,
        max_retries_total=10,
    )
    if budget_backend is not None:
        cfg = ExecutionConfig(
            max_cost_usd=max_cost,
            max_steps=max_steps,
            max_retries_total=10,
            budget_backend=budget_backend,
        )
    return ExecutionContext(config=cfg)


# ---------------------------------------------------------------------------
# 1. TOCTOU: Concurrent reserve() calls exceeding ceiling
# ---------------------------------------------------------------------------


class TestAdversarialConcurrentReserve:
    """Concurrent reserve() calls must not collectively exceed the ceiling."""

    def test_local_concurrent_reserve_no_overflow(self):
        """10 threads each reserving 0.15 against ceiling 1.0 — at most 6 succeed."""
        backend = make_local_backend()
        ceiling = 1.0
        amount = 0.15
        n_threads = 10

        successes: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker():
            try:
                rid = backend.reserve(amount, ceiling)
                with lock:
                    successes.append(rid)
            except OverflowError as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # At most floor(1.0 / 0.15) = 6 reservations can fit
        assert len(successes) <= 6, f"Too many reservations succeeded: {len(successes)}"
        # Total reserved must not exceed ceiling + epsilon
        total_reserved = backend.get_reserved()
        assert total_reserved <= ceiling + _BUDGET_EPSILON * 100, (
            f"Reserved {total_reserved:.8f} exceeds ceiling {ceiling}"
        )

    def test_local_concurrent_reserve_and_commit_ceiling(self):
        """Concurrent reserve+commit pairs must never exceed ceiling."""
        backend = make_local_backend()
        ceiling = 0.5
        amount = 0.1
        n_threads = 20

        committed_costs: list[float] = []
        lock = threading.Lock()

        def worker():
            try:
                rid = backend.reserve(amount, ceiling)
                time.sleep(0.001)  # simulate work between reserve and commit
                total = backend.commit(rid)
                with lock:
                    committed_costs.append(total)
            except OverflowError:
                pass

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final_total = backend.get()
        assert final_total <= ceiling + _BUDGET_EPSILON * 100, (
            f"Committed total {final_total:.8f} exceeds ceiling {ceiling}"
        )

    def test_redis_concurrent_reserve_lua_atomicity(self):
        """Redis Lua script must atomically prevent ceiling overflow under concurrency."""
        fake_client = fakeredis.FakeRedis(decode_responses=True)
        backend = make_redis_backend(fake_client, chain_id="toctou-test")
        ceiling = 1.0
        amount = 0.15
        n_threads = 10

        successes: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker():
            try:
                rid = backend.reserve(amount, ceiling)
                with lock:
                    successes.append(rid)
            except OverflowError as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(successes) <= 6, f"Too many Redis reservations succeeded: {len(successes)}"
        total_reserved = backend.get_reserved()
        assert total_reserved <= ceiling + _BUDGET_EPSILON * 100


# ---------------------------------------------------------------------------
# 2. Double commit / rollback on same reservation ID
# ---------------------------------------------------------------------------


class TestAdversarialDoubleCommitRollback:
    """Double commit or rollback on same reservation must raise KeyError (not silently corrupt)."""

    def test_local_double_commit_raises_key_error(self):
        """Second commit on same rid must raise KeyError."""
        backend = make_local_backend()
        rid = backend.reserve(0.1, 1.0)
        backend.commit(rid)
        with pytest.raises(KeyError):
            backend.commit(rid)

    def test_local_double_rollback_raises_key_error(self):
        """Second rollback on same rid must raise KeyError."""
        backend = make_local_backend()
        rid = backend.reserve(0.1, 1.0)
        backend.rollback(rid)
        with pytest.raises(KeyError):
            backend.rollback(rid)

    def test_local_commit_after_rollback_raises_key_error(self):
        """Commit after rollback must raise KeyError."""
        backend = make_local_backend()
        rid = backend.reserve(0.1, 1.0)
        backend.rollback(rid)
        with pytest.raises(KeyError):
            backend.commit(rid)

    def test_local_rollback_after_commit_raises_key_error(self):
        """Rollback after commit must raise KeyError."""
        backend = make_local_backend()
        rid = backend.reserve(0.1, 1.0)
        backend.commit(rid)
        with pytest.raises(KeyError):
            backend.rollback(rid)

    def test_redis_double_commit_raises_key_error(self):
        """Redis: second commit on same rid must raise KeyError."""
        fake_client = fakeredis.FakeRedis(decode_responses=True)
        backend = make_redis_backend(fake_client, chain_id="dbl-commit")
        rid = backend.reserve(0.1, 1.0)
        backend.commit(rid)
        with pytest.raises(KeyError):
            backend.commit(rid)

    def test_redis_double_rollback_raises_key_error(self):
        """Redis: second rollback on same rid must raise KeyError."""
        fake_client = fakeredis.FakeRedis(decode_responses=True)
        backend = make_redis_backend(fake_client, chain_id="dbl-rollback")
        rid = backend.reserve(0.1, 1.0)
        backend.rollback(rid)
        with pytest.raises(KeyError):
            backend.rollback(rid)

    def test_local_double_commit_does_not_double_charge(self):
        """Even if second commit were to not raise (defensive), cost must not double."""
        backend = make_local_backend()
        rid = backend.reserve(0.3, 1.0)
        backend.commit(rid)
        # First commit should have charged 0.3
        assert abs(backend.get() - 0.3) < 1e-9
        # Second commit must raise; cost must remain 0.3
        with pytest.raises(KeyError):
            backend.commit(rid)
        assert abs(backend.get() - 0.3) < 1e-9


# ---------------------------------------------------------------------------
# 3. Reservation expiry during commit window
# ---------------------------------------------------------------------------


class TestAdversarialReservationExpiry:
    """Reservation that expires before commit must raise KeyError on commit."""

    def test_local_expired_reservation_commit_raises(self, monkeypatch):
        """Commit on expired reservation must raise KeyError."""
        backend = make_local_backend()

        # Patch monotonic to make reservation expire immediately
        original_monotonic = time.monotonic
        call_count = {"n": 0}

        def patched_monotonic():
            call_count["n"] += 1
            # On expire_reservations check, pretend time has advanced far past deadline
            return original_monotonic() + _RESERVATION_TIMEOUT_S + 1.0

        # Reserve with real time to get a valid rid
        rid = backend.reserve(0.1, 1.0)

        # Now fast-forward time so expiry check kicks in
        monkeypatch.setattr(time, "monotonic", patched_monotonic)

        with pytest.raises(KeyError):
            backend.commit(rid)

    def test_local_expired_reservation_rollback_raises(self, monkeypatch):
        """Rollback on expired reservation must raise KeyError."""
        backend = make_local_backend()
        rid = backend.reserve(0.1, 1.0)

        original_monotonic = time.monotonic

        def patched_monotonic():
            return original_monotonic() + _RESERVATION_TIMEOUT_S + 1.0

        monkeypatch.setattr(time, "monotonic", patched_monotonic)

        with pytest.raises(KeyError):
            backend.rollback(rid)

    def test_local_expired_reservation_frees_budget(self, monkeypatch):
        """Expired reservations must be swept and free up budget for new reserves."""
        backend = make_local_backend()
        # Use up almost all budget with reservations
        backend.reserve(0.4, 1.0)
        backend.reserve(0.4, 1.0)

        # At this point, 0.8 is reserved; 0.3 would exceed ceiling
        with pytest.raises(OverflowError):
            backend.reserve(0.3, 1.0)

        # Advance time to expire all reservations
        original_monotonic = time.monotonic

        def patched_monotonic():
            return original_monotonic() + _RESERVATION_TIMEOUT_S + 1.0

        monkeypatch.setattr(time, "monotonic", patched_monotonic)

        # Now expired reservations are swept; 0.3 should succeed
        rid3 = backend.reserve(0.3, 1.0)
        assert rid3 is not None
        assert backend.get_reserved() <= 0.3 + _BUDGET_EPSILON


# ---------------------------------------------------------------------------
# 4. SharedTimeoutPool adversarial tests
# ---------------------------------------------------------------------------


class TestAdversarialSharedTimeoutPool:
    """SharedTimeoutPool must handle callback exceptions and concurrency safely."""

    def test_callback_exception_does_not_crash_pool(self):
        """Callback exception must be swallowed — pool continues running."""
        pool = SharedTimeoutPool()
        fired_after: list[bool] = []
        event = threading.Event()

        def bad_callback():
            raise RuntimeError("intentional crash")

        def good_callback():
            fired_after.append(True)
            event.set()

        pool.schedule(time.monotonic() + 0.01, bad_callback)
        pool.schedule(time.monotonic() + 0.02, good_callback)

        event.wait(timeout=2.0)
        pool.shutdown()

        assert len(fired_after) == 1, "Good callback must fire despite earlier exception"

    def test_cancel_before_fire(self):
        """Cancelled callback must not fire."""
        pool = SharedTimeoutPool()
        fired: list[bool] = []

        def should_not_fire():
            fired.append(True)

        # Schedule very soon, cancel immediately
        handle = pool.schedule(time.monotonic() + 0.5, should_not_fire)
        pool.cancel(handle)

        # Wait beyond the scheduled time to confirm it didn't fire
        time.sleep(0.6)
        pool.shutdown()

        assert len(fired) == 0, "Cancelled callback must not fire"

    def test_cancel_is_idempotent(self):
        """Multiple cancel() calls on same handle must not raise."""
        pool = SharedTimeoutPool()
        handle = pool.schedule(time.monotonic() + 10.0, lambda: None)
        pool.cancel(handle)
        pool.cancel(handle)  # Must not raise
        pool.cancel(handle)
        pool.shutdown()

    def test_schedule_after_shutdown_raises(self):
        """schedule() after shutdown() must raise RuntimeError."""
        pool = SharedTimeoutPool()
        pool.shutdown()
        with pytest.raises(RuntimeError, match="shut down"):
            pool.schedule(time.monotonic() + 1.0, lambda: None)

    def test_rapid_schedule_cancel_stress(self):
        """Rapid schedule/cancel from multiple threads must not deadlock or crash."""
        pool = SharedTimeoutPool()
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker():
            try:
                for _ in range(20):
                    handle = pool.schedule(time.monotonic() + 10.0, lambda: None)
                    pool.cancel(handle)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        pool.shutdown()
        assert len(errors) == 0, f"Errors during rapid schedule/cancel: {errors}"

    def test_daemon_thread_restart_after_crash(self):
        """Pool must restart daemon thread if it dies (e.g., after unhandled signal)."""
        pool = SharedTimeoutPool()
        fired: list[bool] = []
        event = threading.Event()

        def good_callback():
            fired.append(True)
            event.set()

        # Simulate thread having been running and stopped (not directly killable safely,
        # but we can verify lazy restart via is_alive check)
        with pool._lock:
            pool._thread = None  # Force "no thread" state

        # New schedule should start the thread
        pool.schedule(time.monotonic() + 0.05, good_callback)
        event.wait(timeout=2.0)
        pool.shutdown()

        assert len(fired) == 1, "Callback must fire after thread restart"


# ---------------------------------------------------------------------------
# 5. close() idempotency and close() during active _wrap()
# ---------------------------------------------------------------------------


class TestAdversarialContextManagerIdempotency:
    """ExecutionContext context manager / abort() must be idempotent and safe."""

    def test_exit_twice_does_not_raise(self):
        """__exit__ called twice must not raise."""
        ctx = make_ctx()
        ctx.__exit__(None, None, None)
        ctx.__exit__(None, None, None)

    def test_wrap_after_abort_returns_halt(self):
        """wrap_llm_call after abort() must return HALT without executing fn."""
        ctx = make_ctx()
        ctx.abort("test cleanup")

        called: list[bool] = []

        def fn():
            called.append(True)

        decision = ctx.wrap_llm_call(fn)
        assert decision == Decision.HALT
        assert len(called) == 0, "fn must not be called after abort()"

    def test_wrap_after_context_manager_exit_returns_halt(self):
        """wrap_llm_call after __exit__ must return HALT."""
        ctx = make_ctx(max_cost=10.0, max_steps=100)
        ctx.__exit__(None, None, None)

        decision = ctx.wrap_llm_call(lambda: None)
        assert decision == Decision.HALT

    def test_abort_from_other_thread_halts_future_calls(self):
        """abort() called from another thread during _wrap() must halt future calls."""
        ctx = make_ctx(max_cost=10.0, max_steps=100)
        fn_started = threading.Event()
        abort_done = threading.Event()

        def fn():
            fn_started.set()
            abort_done.wait(timeout=2.0)

        def aborter():
            fn_started.wait(timeout=2.0)
            ctx.abort("concurrent abort")
            abort_done.set()

        aborter_thread = threading.Thread(target=aborter)
        aborter_thread.start()

        ctx.wrap_llm_call(fn)
        aborter_thread.join(timeout=3.0)

        # After abort, further wraps must halt
        decision = ctx.wrap_llm_call(lambda: None)
        assert decision == Decision.HALT

    def test_abort_twice_does_not_raise(self):
        """abort() called twice must not raise (idempotent)."""
        ctx = make_ctx()
        ctx.abort("first")
        ctx.abort("second")  # Must not raise

    def test_get_snapshot_after_abort_returns_aborted(self):
        """get_snapshot() after abort() must return aborted=True."""
        ctx = make_ctx()
        ctx.abort("test abort")
        snap = ctx.get_snapshot()
        assert snap.aborted is True
        assert snap.abort_reason == "test abort"

    def test_local_backend_close_is_noop(self):
        """LocalBudgetBackend.close() must be callable multiple times without error."""
        backend = make_local_backend()
        backend.close()
        backend.close()  # Must not raise


# ---------------------------------------------------------------------------
# 6. WrapOptions and ExecutionContext basic robustness
# ---------------------------------------------------------------------------


class TestAdversarialWrapOptionsBasic:
    """WrapOptions must validate inputs correctly."""

    def test_nan_cost_hint_raises_value_error(self):
        """NaN cost_estimate_hint must raise ValueError on construction."""
        with pytest.raises(ValueError):
            WrapOptions(cost_estimate_hint=float("nan"))

    def test_inf_cost_hint_raises_value_error(self):
        """Infinite cost_estimate_hint must raise ValueError on construction."""
        with pytest.raises(ValueError):
            WrapOptions(cost_estimate_hint=float("inf"))

    def test_negative_cost_hint_raises_value_error(self):
        """Negative cost_estimate_hint must raise ValueError on construction."""
        with pytest.raises(ValueError):
            WrapOptions(cost_estimate_hint=-0.1)

    def test_zero_cost_hint_is_valid(self):
        """Zero cost_estimate_hint must be valid (no reservation needed)."""
        opts = WrapOptions(cost_estimate_hint=0.0)
        ctx = make_ctx(max_cost=10.0)
        decision = ctx.wrap_llm_call(fn=lambda: None, options=opts)
        assert decision == Decision.ALLOW

    def test_wrap_with_no_options_uses_defaults(self):
        """wrap_llm_call with no options must use default WrapOptions (0 cost)."""
        ctx = make_ctx(max_cost=10.0)
        decision = ctx.wrap_llm_call(fn=lambda: None)
        assert decision == Decision.ALLOW


# ---------------------------------------------------------------------------
# 7. _nesting_depth_var under deep recursion
# ---------------------------------------------------------------------------


class TestAdversarialNestingDepth:
    """_nesting_depth_var must be correctly decremented even under deep recursion."""

    def test_nesting_depth_resets_after_nested_calls(self):
        """Nesting depth must return to 0 after deeply nested wrap calls complete."""
        ctx = make_ctx(max_cost=100.0, max_steps=1000)

        DEPTH = 50
        call_count = {"n": 0}

        def outer_fn():
            call_count["n"] += 1
            if call_count["n"] < DEPTH:
                ctx.wrap_llm_call(outer_fn, options=WrapOptions(cost_estimate_hint=0.0))

        ctx.wrap_llm_call(outer_fn, options=WrapOptions(cost_estimate_hint=0.0))

        # After all calls complete, depth must be 0
        depth = ctx._nesting_depth_var.get()
        assert depth == 0, f"Nesting depth should be 0 after calls, got {depth}"

    def test_nesting_depth_resets_on_fn_exception(self):
        """Nesting depth must return to 0 even when fn() raises."""
        ctx = make_ctx(max_cost=100.0, max_steps=100)

        def failing_fn():
            raise ValueError("intentional failure")

        ctx.wrap_llm_call(failing_fn)

        depth = ctx._nesting_depth_var.get()
        assert depth == 0, f"Depth must be 0 after exception, got {depth}"


# ---------------------------------------------------------------------------
# 8. Budget epsilon boundary conditions
# ---------------------------------------------------------------------------


class TestAdversarialEpsilonBoundary:
    """Budget ceiling epsilon edge cases must be handled correctly."""

    def test_reserve_at_exact_ceiling_uses_epsilon(self):
        """Reserving exactly ceiling must succeed (epsilon tolerance)."""
        backend = make_local_backend()
        # Reserve exactly ceiling — should succeed with epsilon
        rid = backend.reserve(1.0, 1.0)
        assert rid is not None

    def test_reserve_slightly_over_ceiling_fails(self):
        """Reserving amount > ceiling + epsilon must raise OverflowError."""
        backend = make_local_backend()
        # Amount exceeds ceiling significantly
        with pytest.raises(OverflowError):
            backend.reserve(1.0 + 1e-6, 1.0)

    def test_local_commit_at_exact_ceiling_does_not_overflow(self):
        """Committing at exact ceiling must not raise and must not overflow."""
        backend = make_local_backend()
        rid = backend.reserve(1.0, 1.0)
        total = backend.commit(rid)
        assert total <= 1.0 + _BUDGET_EPSILON * 10

    def test_multiple_small_reserves_accumulate_correctly(self):
        """100 × 0.01 reserves against ceiling 1.0 must succeed; 101st must fail."""
        backend = make_local_backend()
        rids = []
        for i in range(100):
            rid = backend.reserve(0.01, 1.0)
            rids.append(rid)

        with pytest.raises(OverflowError):
            backend.reserve(0.01, 1.0)

        # Cleanup
        for rid in rids:
            backend.rollback(rid)

    def test_nan_amount_raises_value_error(self):
        """NaN amount must raise ValueError immediately."""
        backend = make_local_backend()
        with pytest.raises(ValueError, match="positive and finite"):
            backend.reserve(float("nan"), 1.0)

    def test_inf_amount_raises_value_error(self):
        """Infinite amount must raise ValueError immediately."""
        backend = make_local_backend()
        with pytest.raises(ValueError, match="positive and finite"):
            backend.reserve(float("inf"), 1.0)

    def test_negative_amount_raises_value_error(self):
        """Negative amount must raise ValueError immediately."""
        backend = make_local_backend()
        with pytest.raises(ValueError, match="positive and finite"):
            backend.reserve(-0.1, 1.0)

    def test_zero_amount_raises_value_error(self):
        """Zero amount must raise ValueError immediately."""
        backend = make_local_backend()
        with pytest.raises(ValueError, match="positive and finite"):
            backend.reserve(0.0, 1.0)

    def test_redis_nan_amount_raises_value_error(self):
        """Redis backend: NaN amount must raise ValueError."""
        fake_client = fakeredis.FakeRedis(decode_responses=True)
        backend = make_redis_backend(fake_client, chain_id="epsilon-nan")
        with pytest.raises(ValueError, match="positive and finite"):
            backend.reserve(float("nan"), 1.0)

    def test_redis_reserve_at_exact_ceiling(self):
        """Redis: reserving exactly ceiling must succeed."""
        fake_client = fakeredis.FakeRedis(decode_responses=True)
        backend = make_redis_backend(fake_client, chain_id="epsilon-ceil")
        rid = backend.reserve(1.0, 1.0)
        assert rid is not None


# ---------------------------------------------------------------------------
# 9. ExecutionContext two-phase budget via _wrap
# ---------------------------------------------------------------------------


class TestAdversarialExecutionContextBudget:
    """_wrap() must correctly reserve, commit, or rollback via backend."""

    def test_successful_call_commits_reservation(self):
        """Successful wrap_llm_call must commit the reservation."""
        backend = make_local_backend()
        ctx = make_ctx(max_cost=1.0, budget_backend=backend)

        opts = WrapOptions(cost_estimate_hint=0.1)
        decision = ctx.wrap_llm_call(fn=lambda: None, options=opts)

        assert decision == Decision.ALLOW
        # After commit, get() should reflect cost
        assert abs(backend.get() - 0.1) < 1e-9
        # No pending reservations
        assert backend.get_reserved() == 0.0

    def test_failed_call_rolls_back_reservation(self):
        """fn() raising must roll back reservation — no budget charged."""
        backend = make_local_backend()
        ctx = make_ctx(max_cost=1.0, budget_backend=backend)

        def failing_fn():
            raise ValueError("fail")

        opts = WrapOptions(cost_estimate_hint=0.3)
        ctx.wrap_llm_call(fn=failing_fn, options=opts)

        # After rollback, no cost must be charged
        assert abs(backend.get() - 0.0) < 1e-9
        assert backend.get_reserved() == 0.0

    def test_exception_in_wrap_rolls_back_reservation(self):
        """Exception in fn() must roll back reservation — no budget charged.

        Note: BaseException (non KeyboardInterrupt/SystemExit) is converted to
        Decision.RETRY by _handle_fn_error and does NOT propagate. The key
        invariant is that the reservation is rolled back regardless.
        """
        backend = make_local_backend()
        ctx = make_ctx(max_cost=1.0, budget_backend=backend)

        def raise_runtime():
            raise RuntimeError("unexpected runtime error")

        opts = WrapOptions(cost_estimate_hint=0.2)
        # RuntimeError is caught and converted to Decision.RETRY
        decision = ctx.wrap_llm_call(fn=raise_runtime, options=opts)
        assert decision == Decision.RETRY

        # Reservation must be rolled back — no cost charged
        assert backend.get_reserved() == 0.0
        assert abs(backend.get() - 0.0) < 1e-9

    def test_keyboard_interrupt_in_fn_propagates(self):
        """KeyboardInterrupt must propagate out of _wrap (not be converted to HALT/RETRY)."""
        backend = make_local_backend()
        ctx = make_ctx(max_cost=1.0, budget_backend=backend)

        def raise_kbi():
            raise KeyboardInterrupt

        opts = WrapOptions(cost_estimate_hint=0.2)
        with pytest.raises(KeyboardInterrupt):
            ctx.wrap_llm_call(fn=raise_kbi, options=opts)

        # Reservation must be rolled back even on propagating exception
        assert backend.get_reserved() == 0.0

    def test_reserve_overflow_halts_without_charging(self):
        """reserve() OverflowError path must return HALT and charge nothing."""
        backend = make_local_backend()
        # Pre-fill so ceiling is already exceeded
        backend.add(0.95)

        ctx = make_ctx(max_cost=1.0, budget_backend=backend)
        opts = WrapOptions(cost_estimate_hint=0.1)

        called: list[bool] = []

        def fn():
            called.append(True)

        decision = ctx.wrap_llm_call(fn=fn, options=opts)
        assert decision == Decision.HALT
        assert len(called) == 0, "fn must not be called when reserve overflows"
        # Cost must not increase beyond pre-filled amount
        assert abs(backend.get() - 0.95) < 1e-9

    def test_concurrent_wraps_respect_ceiling(self):
        """Concurrent wrap calls must collectively not exceed budget ceiling."""
        backend = make_local_backend()
        ctx = make_ctx(max_cost=1.0, budget_backend=backend)

        n_threads = 20
        amount = 0.1
        results: list[Decision] = []
        lock = threading.Lock()

        def worker():
            opts = WrapOptions(cost_estimate_hint=amount)
            d = ctx.wrap_llm_call(fn=lambda: None, options=opts)
            with lock:
                results.append(d)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        allow_count = results.count(Decision.ALLOW)
        final_cost = backend.get()

        # At most 10 calls can succeed at 0.1 each against ceiling 1.0
        assert allow_count <= 10, f"Too many ALLOWs: {allow_count}"
        # Total cost must not exceed ceiling
        assert final_cost <= 1.0 + _BUDGET_EPSILON * 100, (
            f"Backend total {final_cost:.8f} exceeds ceiling 1.0"
        )


# ---------------------------------------------------------------------------
# 10. Redis Lua script atomicity under concurrent reserve+commit+rollback
# ---------------------------------------------------------------------------


class TestAdversarialRedisLuaAtomicity:
    """Redis Lua scripts must be atomic and prevent double-spend."""

    def test_concurrent_redis_reserve_does_not_overflow(self):
        """20 concurrent threads reserving 0.06 against ceiling 1.0 — max 16 succeed."""
        fake_client = fakeredis.FakeRedis(decode_responses=True)

        # Use separate backends in same fake Redis (shared via same client)
        successes: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker():
            # Each worker creates its own backend pointing to the same fake Redis
            backend = make_redis_backend(fake_client, chain_id="concurrent-lua")
            try:
                rid = backend.reserve(0.06, 1.0)
                with lock:
                    successes.append(rid)
            except OverflowError as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 20 * 0.06 = 1.2 > 1.0 — some must fail
        assert len(errors) > 0, "Some reservations must fail to prevent overflow"
        # Total reserved in Redis must not exceed ceiling
        backend_check = make_redis_backend(fake_client, chain_id="concurrent-lua")
        total_reserved = backend_check.get_reserved()
        assert total_reserved <= 1.0 + _BUDGET_EPSILON * 100, (
            f"Redis reserved {total_reserved:.8f} exceeds ceiling"
        )

    def test_redis_commit_and_rollback_lua_consistency(self):
        """After commit + rollback of different reservations, totals must be consistent."""
        fake_client = fakeredis.FakeRedis(decode_responses=True)
        backend = make_redis_backend(fake_client, chain_id="commit-rollback-lua")

        rid1 = backend.reserve(0.3, 1.0)
        rid2 = backend.reserve(0.2, 1.0)

        backend.commit(rid1)  # charge 0.3
        backend.rollback(rid2)  # release 0.2 without charge

        committed = backend.get()
        reserved = backend.get_reserved()

        assert abs(committed - 0.3) < 1e-6, f"Expected 0.3, got {committed}"
        assert reserved == 0.0, f"Reserved should be 0, got {reserved}"


# ---------------------------------------------------------------------------
# 11. LocalBudgetBackend.reset() clears reservations
# ---------------------------------------------------------------------------


class TestAdversarialReset:
    """reset() must clear all reservations and committed cost."""

    def test_reset_clears_reservations_and_cost(self):
        """reset() must clear both committed and pending reservations."""
        backend = make_local_backend()
        backend.add(0.5)
        rid = backend.reserve(0.3, 1.0)

        backend.reset()

        assert backend.get() == 0.0
        assert backend.get_reserved() == 0.0

        # Former reservation must be gone
        with pytest.raises(KeyError):
            backend.commit(rid)

    def test_redis_reset_clears_reservations(self):
        """Redis reset() must delete committed key and reservations hash."""
        fake_client = fakeredis.FakeRedis(decode_responses=True)
        backend = make_redis_backend(fake_client, chain_id="reset-test")

        backend.add(0.5)
        backend.reserve(0.3, 1.0)

        backend.reset()

        assert abs(backend.get() - 0.0) < 1e-9
        assert backend.get_reserved() == 0.0


# ---------------------------------------------------------------------------
# 12. get_reserved() correctness
# ---------------------------------------------------------------------------


class TestAdversarialGetReserved:
    """get_reserved() must accurately reflect currently held reservations."""

    def test_get_reserved_before_and_after_commit(self):
        """get_reserved() must decrease after commit."""
        backend = make_local_backend()
        rid = backend.reserve(0.4, 1.0)

        reserved_before = backend.get_reserved()
        assert abs(reserved_before - 0.4) < 1e-9

        backend.commit(rid)

        reserved_after = backend.get_reserved()
        assert reserved_after == 0.0

    def test_get_reserved_multiple_rids(self):
        """get_reserved() must sum all active reservations."""
        backend = make_local_backend()
        backend.reserve(0.1, 1.0)
        rid2 = backend.reserve(0.2, 1.0)
        backend.reserve(0.15, 1.0)

        total = backend.get_reserved()
        assert abs(total - 0.45) < 1e-9

        backend.rollback(rid2)
        total2 = backend.get_reserved()
        assert abs(total2 - 0.25) < 1e-9
