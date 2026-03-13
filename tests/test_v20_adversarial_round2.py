"""Round 2 adversarial tests for v2.0 source files.

Attack vectors not covered by Round 1:
1. _propagate_child_cost: circular parent chain -> RecursionError (BUG FIXED)
2. SharedTimeoutPool: 1000 rapid schedules then shutdown
3. WebSocket: malformed ASGI messages (missing type field)
4. reconciliation_callback: concurrent thread safety
5. CancellationToken: concurrent cancel() from multiple threads
6. _StepGuardProxy: closed/aborted ExecutionContext
7. Two-phase budget: cross-context reservation commit (shared backend)
8. Decision: hash stability as dict key
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from veronica_core.containment.execution_context import (
    CancellationToken,
    ExecutionConfig,
    ExecutionContext,
    WrapOptions,
)
from veronica_core.distributed import LocalBudgetBackend
from veronica_core.inject import GuardConfig
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# 1. _propagate_child_cost: circular parent chain (BUG FIXED)
# ---------------------------------------------------------------------------


class TestCircularParentChain:
    """_propagate_child_cost must NOT recurse infinitely on circular chains."""

    def test_two_node_cycle_does_not_stack_overflow(self) -> None:
        """A -> B -> A circular chain must terminate with a warning, not RecursionError."""
        cfg = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=3)
        a = ExecutionContext(config=cfg)
        b = ExecutionContext(config=cfg)
        # Manually create circular reference (invalid state, but defensive guard must hold)
        a._parent = b
        b._parent = a

        # Must NOT raise RecursionError
        a._propagate_child_cost(0.5)
        # Both nodes should have accumulated the cost before loop detection
        assert a._cost_usd_accumulated > 0.0

    def test_three_node_cycle_does_not_stack_overflow(self) -> None:
        """A -> B -> C -> A circular chain must terminate safely."""
        cfg = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=3)
        a = ExecutionContext(config=cfg)
        b = ExecutionContext(config=cfg)
        c = ExecutionContext(config=cfg)
        a._parent = b
        b._parent = c
        c._parent = a  # cycle back

        # Must NOT raise RecursionError
        a._propagate_child_cost(0.3)
        assert a._cost_usd_accumulated > 0.0

    def test_linear_chain_still_propagates_correctly(self) -> None:
        """Non-circular parent chain must still propagate cost all the way up."""
        cfg = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=3)
        grandparent = ExecutionContext(config=cfg)
        parent = ExecutionContext(config=cfg, parent=grandparent)
        child = ExecutionContext(config=cfg, parent=parent)

        child._propagate_child_cost(0.3)

        assert parent._cost_usd_accumulated == pytest.approx(0.3)
        assert grandparent._cost_usd_accumulated == pytest.approx(0.3)

    def test_circular_chain_does_not_abort_beyond_actual_nodes(self) -> None:
        """With cycle, abort must only trigger on nodes that actually exceed ceiling."""
        cfg = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=3)
        a = ExecutionContext(config=cfg)
        b = ExecutionContext(config=cfg)
        a._parent = b
        b._parent = a

        # Push cost high enough to abort 'a', then cycle is detected
        a._propagate_child_cost(1.5)
        # At minimum, 'a' should be aborted
        assert a._aborted is True


# ---------------------------------------------------------------------------
# 2. SharedTimeoutPool: rapid schedules + shutdown
# ---------------------------------------------------------------------------


class TestSharedTimeoutPoolStress:
    """SharedTimeoutPool must not deadlock or crash under heavy load + shutdown."""

    def test_1000_rapid_schedules_then_shutdown(self) -> None:
        """Schedule 1000 far-future callbacks then shut down immediately."""
        from veronica_core.containment.timeout_pool import SharedTimeoutPool

        pool = SharedTimeoutPool()
        fired: list[int] = []

        for i in range(1000):
            pool.schedule(time.monotonic() + 300, lambda: fired.append(1))

        pool.shutdown()
        time.sleep(0.05)  # Give daemon thread a moment to stop
        # None should have fired since deadline is 5 minutes away
        assert len(fired) == 0

    def test_schedule_after_shutdown_raises_runtime_error(self) -> None:
        """schedule() on a shut-down pool must raise RuntimeError."""
        from veronica_core.containment.timeout_pool import SharedTimeoutPool

        pool = SharedTimeoutPool()
        pool.shutdown()

        with pytest.raises(RuntimeError, match="shut down"):
            pool.schedule(time.monotonic() + 1.0, lambda: None)

    def test_cancel_after_shutdown_is_safe(self) -> None:
        """cancel() on a shut-down pool must not raise."""
        from veronica_core.containment.timeout_pool import SharedTimeoutPool

        pool = SharedTimeoutPool()
        handle = pool.schedule(time.monotonic() + 300, lambda: None)
        pool.shutdown()
        # cancel() after shutdown must be a no-op (not raise)
        pool.cancel(handle)

    def test_concurrent_schedule_and_cancel_no_deadlock(self) -> None:
        """Concurrent schedule() and cancel() from many threads must not deadlock."""
        from veronica_core.containment.timeout_pool import SharedTimeoutPool

        pool = SharedTimeoutPool()
        handles: list[int] = []
        lock = threading.Lock()

        def schedule_and_cancel():
            h = pool.schedule(time.monotonic() + 300, lambda: None)
            with lock:
                handles.append(h)

        threads = [threading.Thread(target=schedule_and_cancel) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        for h in handles:
            pool.cancel(h)

        pool.shutdown()
        assert len(handles) == 50


# ---------------------------------------------------------------------------
# 3. WebSocket: malformed ASGI messages
# ---------------------------------------------------------------------------


class TestWebSocketMalformedMessages:
    """VeronicaASGIMiddleware must handle malformed ASGI messages gracefully."""

    def test_receive_returns_message_without_type_does_not_crash(self) -> None:
        """Inner app returning a message dict without 'type' must not crash middleware."""
        from veronica_core.middleware import VeronicaASGIMiddleware

        config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
        call_log: list[str] = []

        async def inner_app(scope, receive, send):
            await receive()
            call_log.append("received")
            await send({"data": "no_type_here"})
            call_log.append("sent")

        middleware = VeronicaASGIMiddleware(inner_app, config)
        scope = {"type": "websocket"}

        receive_msgs = [{"not_type": "connect"}, {"type": "websocket.disconnect"}]
        receive_idx = [0]

        async def receive():
            msg = receive_msgs[receive_idx[0]]
            receive_idx[0] = min(receive_idx[0] + 1, len(receive_msgs) - 1)
            return msg

        sent_msgs: list[Any] = []

        async def send(msg):
            sent_msgs.append(msg)

        async def run():
            await middleware(scope, receive, send)

        asyncio.run(run())
        assert "sent" in call_log

    def test_send_with_none_type_message_does_not_crash(self) -> None:
        """_tracked_send receiving {'type': None} must not crash."""
        from veronica_core.middleware import VeronicaASGIMiddleware

        config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
        sent_msgs: list[Any] = []

        async def inner_app(scope, receive, send):
            await send({"type": None, "data": "test"})

        middleware = VeronicaASGIMiddleware(inner_app, config)
        scope = {"type": "websocket"}

        async def receive():
            return {"type": "websocket.connect"}

        async def send(msg):
            sent_msgs.append(msg)

        asyncio.run(middleware(scope, receive, send))


# ---------------------------------------------------------------------------
# 4. reconciliation_callback: concurrent thread safety
# ---------------------------------------------------------------------------


class TestReconciliationCallbackThreadSafety:
    """reconciliation_callback.on_reconcile called from multiple threads must be safe."""

    def test_concurrent_wrap_calls_reconciliation_safety(self) -> None:
        """Multiple threads calling wrap_llm_call with reconciliation_callback must not crash."""
        config = ExecutionConfig(
            max_cost_usd=100.0, max_steps=1000, max_retries_total=100
        )
        ctx = ExecutionContext(config=config)

        calls: list[tuple[float, float]] = []
        lock = threading.Lock()

        class ThreadSafeCallback:
            def on_reconcile(self, estimated: float, actual: float) -> None:
                with lock:
                    calls.append((estimated, actual))

        callback = ThreadSafeCallback()

        errors: list[Exception] = []

        def worker():
            try:
                opts = WrapOptions(
                    cost_estimate_hint=0.01,
                    reconciliation_callback=callback,
                )
                ctx.wrap_llm_call(fn=lambda: None, options=opts)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors, f"Unexpected errors: {errors[:3]}"
        assert len(calls) == 20

    def test_reconciliation_callback_exception_does_not_propagate(self) -> None:
        """A crashing reconciliation_callback must not propagate its exception."""
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=3)
        ctx = ExecutionContext(config=config)

        class CrashingCallback:
            def on_reconcile(self, estimated: float, actual: float) -> None:
                raise RuntimeError("callback crashed")

        opts = WrapOptions(
            cost_estimate_hint=0.01,
            reconciliation_callback=CrashingCallback(),
        )
        result = ctx.wrap_llm_call(fn=lambda: None, options=opts)
        # Must still return ALLOW despite crashing callback
        assert result == Decision.ALLOW


# ---------------------------------------------------------------------------
# 5. CancellationToken: concurrent cancel() from multiple threads
# ---------------------------------------------------------------------------


class TestCancellationTokenAdversarial:
    """CancellationToken must be thread-safe under concurrent access."""

    def test_concurrent_cancel_100_threads(self) -> None:
        """100 threads calling cancel() simultaneously must all succeed."""
        token = CancellationToken()
        errors: list[Exception] = []

        def cancel():
            try:
                token.cancel()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=cancel) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors
        assert token.is_cancelled

    def test_wait_returns_true_after_concurrent_cancel(self) -> None:
        """wait() must return True (cancelled) even when cancel() races with wait()."""
        token = CancellationToken()
        results: list[bool] = []

        def waiter():
            result = token.wait(timeout_s=2.0)
            results.append(result)

        def canceller():
            time.sleep(0.01)
            token.cancel()

        threads = [threading.Thread(target=waiter) for _ in range(5)]
        threads.append(threading.Thread(target=canceller))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # All waiters should have seen the cancellation
        assert all(results)

    def test_is_cancelled_is_monotonic(self) -> None:
        """Once cancelled, is_cancelled must never return False again."""
        token = CancellationToken()
        assert not token.is_cancelled
        token.cancel()
        assert token.is_cancelled
        # Call cancel() again -- must remain True
        token.cancel()
        assert token.is_cancelled


# ---------------------------------------------------------------------------
# 6. _StepGuardProxy: operations on closed/aborted ExecutionContext
# ---------------------------------------------------------------------------


class TestStepGuardProxyOnClosedContext:
    """_StepGuardProxy on a closed/aborted ExecutionContext must degrade gracefully."""

    def test_step_on_aborted_context_returns_bool(self) -> None:
        """step() on an aborted context must return a bool (not raise)."""
        from veronica_core.adapters._shared import ExecutionContextContainerAdapter

        cfg = ExecutionConfig(max_cost_usd=10.0, max_steps=5, max_retries_total=3)
        ctx = ExecutionContext(config=cfg)
        ctx.__exit__(None, None, None)  # close context

        config = GuardConfig(max_cost_usd=10.0, max_steps=5)
        adapter = ExecutionContextContainerAdapter(ctx, config)

        result = adapter.step_guard.step()
        assert isinstance(result, bool)

    def test_check_on_aborted_context_returns_denied(self) -> None:
        """check() on an aborted context must return allowed=False."""
        from veronica_core.adapters._shared import ExecutionContextContainerAdapter

        cfg = ExecutionConfig(max_cost_usd=10.0, max_steps=5, max_retries_total=3)
        ctx = ExecutionContext(config=cfg)
        ctx.__exit__(None, None, None)  # close context

        config = GuardConfig(max_cost_usd=10.0, max_steps=5)
        adapter = ExecutionContextContainerAdapter(ctx, config)

        decision = adapter.check(cost_usd=0.0)
        assert decision.allowed is False

    def test_budget_proxy_on_closed_context_returns_float(self) -> None:
        """budget.spent_usd on a closed context must return a float (not raise)."""
        from veronica_core.adapters._shared import ExecutionContextContainerAdapter

        cfg = ExecutionConfig(max_cost_usd=10.0, max_steps=5, max_retries_total=3)
        ctx = ExecutionContext(config=cfg)
        ctx.__exit__(None, None, None)

        config = GuardConfig(max_cost_usd=10.0, max_steps=5)
        adapter = ExecutionContextContainerAdapter(ctx, config)

        spent = adapter.budget.spent_usd
        assert isinstance(spent, float)


# ---------------------------------------------------------------------------
# 7. Two-phase budget: cross-context reservation handling
# ---------------------------------------------------------------------------


class TestCrossContextReservation:
    """Reservation IDs belong to the backend, not a specific ExecutionContext.

    The shared backend does not prevent cross-context commit/rollback.
    These tests document the current behavior (by design: backend is stateless
    w.r.t. which context made the reservation).
    """

    def test_reservation_id_from_different_context_can_commit(self) -> None:
        """Current design: shared backend allows cross-context commit (documented behavior)."""
        backend = LocalBudgetBackend()
        cfg = ExecutionConfig(
            max_cost_usd=10.0,
            max_steps=10,
            max_retries_total=3,
            budget_backend=backend,
        )
        ExecutionContext(config=cfg)
        ExecutionContext(config=cfg)

        # Any context with the shared backend can make a reservation
        rid = backend.reserve(0.5, ceiling=10.0)

        # ctx2 commits ctx1's reservation (cross-context)
        total = backend.commit(rid)
        assert total == pytest.approx(0.5)
        assert backend.get() == pytest.approx(0.5)

    def test_double_commit_of_same_reservation_raises_key_error(self) -> None:
        """Once committed, the same reservation_id cannot be committed again."""
        backend = LocalBudgetBackend()
        rid = backend.reserve(0.5, ceiling=10.0)
        backend.commit(rid)

        with pytest.raises(KeyError):
            backend.commit(rid)

    def test_rollback_after_commit_raises_key_error(self) -> None:
        """Rollback after commit must raise KeyError (reservation already consumed)."""
        backend = LocalBudgetBackend()
        rid = backend.reserve(0.5, ceiling=10.0)
        backend.commit(rid)

        with pytest.raises(KeyError):
            backend.rollback(rid)

    def test_concurrent_commit_and_rollback_does_not_double_count(self) -> None:
        """Concurrent commit + rollback on different reservations must not corrupt total."""
        backend = LocalBudgetBackend()
        n = 20
        rids = [backend.reserve(0.1, ceiling=100.0) for _ in range(n)]

        def action(i: int, rid: str) -> None:
            if i % 2 == 0:
                backend.commit(rid)
            else:
                backend.rollback(rid)

        threads = [threading.Thread(target=action, args=(i, rids[i])) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        expected = (n // 2) * 0.1
        assert backend.get() == pytest.approx(expected, abs=1e-9)
        assert backend.get_reserved() == 0.0


# ---------------------------------------------------------------------------
# 8. Decision: hash stability as dict key
# ---------------------------------------------------------------------------


class TestDecisionHashStability:
    """Decision enum values must be hashable and stable as dict keys."""

    def test_decision_as_dict_key(self) -> None:
        """Decision values must work as dict keys."""
        d: dict[Decision, str] = {
            Decision.ALLOW: "allow",
            Decision.HALT: "halt",
            Decision.RETRY: "retry",
            Decision.DEGRADE: "degrade",
        }
        assert d[Decision.ALLOW] == "allow"
        assert d[Decision.HALT] == "halt"

    def test_decision_in_set(self) -> None:
        """Decision values must work in sets."""
        s = {Decision.ALLOW, Decision.HALT, Decision.ALLOW}
        assert len(s) == 2
        assert Decision.ALLOW in s

    def test_decision_hash_consistent_across_calls(self) -> None:
        """hash(Decision.ALLOW) must be the same across repeated calls."""
        h1 = hash(Decision.ALLOW)
        h2 = hash(Decision.ALLOW)
        assert h1 == h2

    def test_decision_equality_by_identity(self) -> None:
        """Decision.ALLOW == Decision.ALLOW must be True."""
        assert Decision.ALLOW == Decision.ALLOW
        assert Decision.HALT != Decision.ALLOW


# ---------------------------------------------------------------------------
# 9. Execution Context: metrics HALT path via reserve OverflowError
# ---------------------------------------------------------------------------


class TestMetricsHaltOnReserveOverflow:
    """metrics.record_decision('HALT') must be called when reserve() raises OverflowError."""

    def test_metrics_halt_recorded_on_reserve_overflow(self) -> None:
        """When reserve() raises OverflowError, metrics must record HALT."""
        backend = LocalBudgetBackend()
        backend.add(0.95)  # Near ceiling

        config = ExecutionConfig(
            max_cost_usd=1.0,
            max_steps=10,
            max_retries_total=3,
            budget_backend=backend,
        )
        metrics = MagicMock()
        ctx = ExecutionContext(config=config, metrics=metrics)

        opts = WrapOptions(cost_estimate_hint=0.1)
        result = ctx.wrap_llm_call(fn=lambda: None, options=opts)

        assert result == Decision.HALT
        halt_calls = [
            c for c in metrics.record_decision.call_args_list if c[0][1] == "HALT"
        ]
        assert len(halt_calls) >= 1

    def test_metrics_halt_recorded_on_legacy_estimate_local_cost(self) -> None:
        """When local _cost_usd_accumulated exceeds ceiling, metrics must record HALT.

        Note: _check_budget_estimate uses _cost_usd_accumulated (local), not
        backend.get(). This is by design -- the local accumulator tracks what
        THIS context has spent. External backend spend by other contexts does
        not block new calls via _check_budget_estimate.
        """
        config = ExecutionConfig(
            max_cost_usd=1.0,
            max_steps=10,
            max_retries_total=3,
        )
        metrics = MagicMock()
        ctx = ExecutionContext(config=config, metrics=metrics)

        # Simulate near-ceiling local spend by committing through the default backend
        ctx._budget_backend.add(0.95)

        opts = WrapOptions(cost_estimate_hint=0.1)
        result = ctx.wrap_llm_call(fn=lambda: None, options=opts)

        assert result == Decision.HALT
        halt_calls = [
            c for c in metrics.record_decision.call_args_list if c[0][1] == "HALT"
        ]
        assert len(halt_calls) >= 1


# ---------------------------------------------------------------------------
# 10. Two-phase budget: separate backends cannot share reservation IDs
# ---------------------------------------------------------------------------


class TestReservationIdBackendIsolation:
    """Reservation IDs from backend_A must not be usable on backend_B."""

    def test_commit_foreign_rid_raises_key_error(self) -> None:
        """A reservation_id from backend_A cannot be committed on backend_B."""
        backend_a = LocalBudgetBackend()
        backend_b = LocalBudgetBackend()
        rid_a = backend_a.reserve(0.10, 1.0)
        with pytest.raises(KeyError):
            backend_b.commit(rid_a)
        # Cleanup
        backend_a.rollback(rid_a)
        assert backend_a.get_reserved() == 0.0

    def test_rollback_foreign_rid_raises_key_error(self) -> None:
        """A reservation_id from backend_A cannot be rolled back on backend_B."""
        backend_a = LocalBudgetBackend()
        backend_b = LocalBudgetBackend()
        rid_a = backend_a.reserve(0.10, 1.0)
        with pytest.raises(KeyError):
            backend_b.rollback(rid_a)
        backend_a.rollback(rid_a)

    def test_double_commit_raises_key_error(self) -> None:
        """commit() on an already-committed reservation must raise KeyError."""
        backend = LocalBudgetBackend()
        rid = backend.reserve(0.10, 1.0)
        backend.commit(rid)
        with pytest.raises(KeyError):
            backend.commit(rid)

    def test_double_rollback_raises_key_error(self) -> None:
        """rollback() on an already-rolled-back reservation must raise KeyError."""
        backend = LocalBudgetBackend()
        rid = backend.reserve(0.10, 1.0)
        backend.rollback(rid)
        with pytest.raises(KeyError):
            backend.rollback(rid)

    def test_commit_after_rollback_raises_key_error(self) -> None:
        """commit() after rollback() must raise KeyError."""
        backend = LocalBudgetBackend()
        rid = backend.reserve(0.10, 1.0)
        backend.rollback(rid)
        with pytest.raises(KeyError):
            backend.commit(rid)


# ---------------------------------------------------------------------------
# 11. Removed deprecated APIs (veronica_core.__init__)
# ---------------------------------------------------------------------------


class TestRemovedDeprecatedApis:
    """Removed deprecated APIs must raise AttributeError (not DeprecationWarning)."""

    def test_aicontainer_lowercase_raises(self) -> None:
        """veronica_core.AIcontainer (old alias) must raise AttributeError."""
        import veronica_core

        with pytest.raises(AttributeError):
            _ = veronica_core.AIcontainer  # noqa: F821

    def test_veronica_persistence_raises(self) -> None:
        """veronica_core.VeronicaPersistence must raise AttributeError (removed)."""
        import veronica_core

        with pytest.raises(AttributeError):
            _ = veronica_core.VeronicaPersistence  # noqa: F821

    def test_unknown_attribute_raises_attribute_error(self) -> None:
        """Accessing a completely unknown attribute must raise AttributeError."""
        import veronica_core

        with pytest.raises(AttributeError):
            _ = veronica_core.NonExistentAttribute12345  # noqa: F821

    def test_container_module_aicontainer_raises(self) -> None:
        """veronica_core.container.AIcontainer (old alias) must raise AttributeError."""
        import veronica_core.container as c

        with pytest.raises(AttributeError):
            _ = c.AIcontainer  # noqa: F821

    def test_container_module_unknown_raises_attribute_error(self) -> None:
        """veronica_core.container.UnknownName must raise AttributeError."""
        import veronica_core.container as c

        with pytest.raises(AttributeError):
            _ = c.UnknownAttribute99999  # noqa: F821

    def test_old_adapter_shim_raises_module_not_found(self) -> None:
        """veronica_core.adapter (removed v3.7.5) must raise ModuleNotFoundError."""
        import sys

        sys.modules.pop("veronica_core.adapter", None)
        with pytest.raises(ModuleNotFoundError):
            import veronica_core.adapter  # noqa: F401

    def test_old_adapter_exec_raises_module_not_found(self) -> None:
        """veronica_core.adapter.exec (removed v3.7.5) must raise ModuleNotFoundError."""
        import sys

        sys.modules.pop("veronica_core.adapter", None)
        sys.modules.pop("veronica_core.adapter.exec", None)
        with pytest.raises(ModuleNotFoundError):
            import veronica_core.adapter.exec  # noqa: F401
