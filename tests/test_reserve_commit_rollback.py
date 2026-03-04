"""Adversarial tests for reserve/commit/rollback budget protocol (Item 4).

Tests cover:
1. LocalBudgetBackend: basic reserve/commit/rollback
2. LocalBudgetBackend: concurrent reserve races
3. LocalBudgetBackend: double commit, rollback after commit, reservation timeout
4. RedisBudgetBackend: atomic reserve via Lua, commit, rollback
5. RedisBudgetBackend: fallback on Redis failure
6. ExecutionContext: two-phase integration with _wrap()
7. Item 1c: CancellationToken signalled by _propagate_child_cost
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import fakeredis
import pytest

from veronica_core.distributed import (
    LocalBudgetBackend,
    RedisBudgetBackend,
)
from veronica_core.containment.execution_context import (
    ExecutionContext,
    ExecutionConfig,
    WrapOptions,
)
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_redis_backend(fake_client, chain_id: str = "test") -> RedisBudgetBackend:
    """Create RedisBudgetBackend with injected fakeredis client."""
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


# ---------------------------------------------------------------------------
# LocalBudgetBackend: basic
# ---------------------------------------------------------------------------


class TestLocalReserveCommitRollback:
    def test_reserve_holds_amount(self):
        b = LocalBudgetBackend()
        rid = b.reserve(0.5, ceiling=1.0)
        assert rid is not None
        # Committed total unchanged — only escrowed
        assert b.get() == 0.0
        assert b.get_reserved() == pytest.approx(0.5)

    def test_commit_adds_to_committed(self):
        b = LocalBudgetBackend()
        rid = b.reserve(0.5, ceiling=1.0)
        total = b.commit(rid)
        assert total == pytest.approx(0.5)
        assert b.get() == pytest.approx(0.5)
        assert b.get_reserved() == 0.0

    def test_rollback_releases_escrow(self):
        b = LocalBudgetBackend()
        rid = b.reserve(0.5, ceiling=1.0)
        b.rollback(rid)
        assert b.get() == 0.0
        assert b.get_reserved() == 0.0

    def test_reserve_respects_committed_plus_reserved(self):
        b = LocalBudgetBackend()
        b.add(0.6)
        with pytest.raises(OverflowError):
            b.reserve(0.5, ceiling=1.0)

    def test_reserve_multiple_reservations_ceiling_check(self):
        b = LocalBudgetBackend()
        b.reserve(0.3, ceiling=1.0)
        b.reserve(0.3, ceiling=1.0)
        with pytest.raises(OverflowError):
            b.reserve(0.5, ceiling=1.0)

    def test_double_commit_raises_key_error(self):
        b = LocalBudgetBackend()
        rid = b.reserve(0.5, ceiling=1.0)
        b.commit(rid)
        with pytest.raises(KeyError):
            b.commit(rid)

    def test_rollback_after_commit_raises_key_error(self):
        b = LocalBudgetBackend()
        rid = b.reserve(0.5, ceiling=1.0)
        b.commit(rid)
        with pytest.raises(KeyError):
            b.rollback(rid)

    def test_unknown_reservation_commit_raises_key_error(self):
        b = LocalBudgetBackend()
        with pytest.raises(KeyError):
            b.commit("nonexistent-id")

    def test_unknown_reservation_rollback_raises_key_error(self):
        b = LocalBudgetBackend()
        with pytest.raises(KeyError):
            b.rollback("nonexistent-id")

    def test_reserve_zero_raises_value_error(self):
        b = LocalBudgetBackend()
        with pytest.raises(ValueError):
            b.reserve(0.0, ceiling=1.0)

    def test_reserve_negative_raises_value_error(self):
        b = LocalBudgetBackend()
        with pytest.raises(ValueError):
            b.reserve(-0.1, ceiling=1.0)

    def test_reset_clears_reservations(self):
        b = LocalBudgetBackend()
        b.reserve(0.5, ceiling=1.0)
        b.reset()
        assert b.get() == 0.0
        assert b.get_reserved() == 0.0


# ---------------------------------------------------------------------------
# LocalBudgetBackend: reservation expiry
# ---------------------------------------------------------------------------


class TestLocalReservationExpiry:
    def test_expired_reservation_auto_rolled_back(self, monkeypatch):
        b = LocalBudgetBackend()
        # Monkey-patch _RESERVATION_TIMEOUT_S to 0 so reservation expires immediately
        import veronica_core.distributed as dm
        original = dm._RESERVATION_TIMEOUT_S
        monkeypatch.setattr(dm, "_RESERVATION_TIMEOUT_S", 0.0)
        rid = b.reserve(0.5, ceiling=1.0)
        # Expire by waiting 1 tick
        time.sleep(0.01)
        # Next reserve() should see expired slot freed
        rid2 = b.reserve(0.5, ceiling=1.0)
        assert rid2 != rid
        monkeypatch.setattr(dm, "_RESERVATION_TIMEOUT_S", original)

    def test_commit_expired_reservation_raises_key_error(self, monkeypatch):
        b = LocalBudgetBackend()
        import veronica_core.distributed as dm
        original = dm._RESERVATION_TIMEOUT_S
        monkeypatch.setattr(dm, "_RESERVATION_TIMEOUT_S", 0.0)
        rid = b.reserve(0.5, ceiling=1.0)
        time.sleep(0.01)
        # Trigger expiry sweep by calling get_reserved()
        b.get_reserved()
        # Now commit should raise because reservation was removed
        with pytest.raises(KeyError):
            b.commit(rid)
        monkeypatch.setattr(dm, "_RESERVATION_TIMEOUT_S", original)


# ---------------------------------------------------------------------------
# LocalBudgetBackend: concurrent reserve races (adversarial)
# ---------------------------------------------------------------------------


class TestAdversarialConcurrentReserve:
    def test_concurrent_reserve_ceiling_enforced(self):
        """10 threads compete for a $1.0 ceiling with $0.15 each. At most 6 should succeed."""
        b = LocalBudgetBackend()
        successful = []
        failed = []
        lock = threading.Lock()

        def try_reserve():
            try:
                rid = b.reserve(0.15, ceiling=1.0)
                with lock:
                    successful.append(rid)
            except OverflowError:
                with lock:
                    failed.append(True)

        threads = [threading.Thread(target=try_reserve) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # At most 6 reservations of $0.15 fit in $1.0 (6*0.15=0.90, 7*0.15=1.05 > 1.0)
        assert len(successful) <= 6
        total_reserved = b.get_reserved()
        assert total_reserved <= 1.0 + 1e-9

    def test_concurrent_commit_rollback_no_double_count(self):
        """Reserve N slots, half commit, half rollback. Final total must match commits only."""
        b = LocalBudgetBackend()
        n = 20
        rids = [b.reserve(0.1, ceiling=100.0) for _ in range(n)]

        def action(i, rid):
            if i % 2 == 0:
                b.commit(rid)
            else:
                b.rollback(rid)

        threads = [threading.Thread(target=action, args=(i, rids[i])) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected = (n // 2) * 0.1
        assert b.get() == pytest.approx(expected, abs=1e-9)
        assert b.get_reserved() == 0.0

    def test_race_between_reserve_and_ceiling_check(self):
        """Concurrent reserve+commit should never allow total to exceed ceiling."""
        ceiling = 1.0
        b = LocalBudgetBackend()
        errors = []
        committed_total = []
        lock = threading.Lock()

        def worker():
            try:
                rid = b.reserve(0.1, ceiling=ceiling)
                total = b.commit(rid)
                with lock:
                    committed_total.append(total)
            except OverflowError:
                pass
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        assert b.get() <= ceiling + 1e-9


# ---------------------------------------------------------------------------
# RedisBudgetBackend: reserve/commit/rollback via Lua
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis_client():
    server = fakeredis.FakeServer()
    return fakeredis.FakeRedis(server=server, decode_responses=True)


class TestRedisReserveCommitRollback:
    def test_reserve_commit_basic(self, fake_redis_client):
        b = make_redis_backend(fake_redis_client)
        rid = b.reserve(0.5, ceiling=1.0)
        assert rid is not None
        total = b.commit(rid)
        assert float(total) == pytest.approx(0.5)

    def test_reserve_rollback_basic(self, fake_redis_client):
        b = make_redis_backend(fake_redis_client)
        rid = b.reserve(0.5, ceiling=1.0)
        b.rollback(rid)
        assert b.get() == 0.0

    def test_reserve_ceiling_enforced(self, fake_redis_client):
        b = make_redis_backend(fake_redis_client)
        b.reserve(0.6, ceiling=1.0)
        with pytest.raises(OverflowError):
            b.reserve(0.5, ceiling=1.0)

    def test_commit_unknown_reservation_raises_key_error(self, fake_redis_client):
        b = make_redis_backend(fake_redis_client)
        with pytest.raises(KeyError):
            b.commit("nonexistent")

    def test_rollback_unknown_reservation_raises_key_error(self, fake_redis_client):
        b = make_redis_backend(fake_redis_client)
        with pytest.raises(KeyError):
            b.rollback("nonexistent")

    def test_reserve_multiple_ceiling_aggregate(self, fake_redis_client):
        b = make_redis_backend(fake_redis_client)
        rid1 = b.reserve(0.3, ceiling=1.0)
        rid2 = b.reserve(0.3, ceiling=1.0)
        with pytest.raises(OverflowError):
            b.reserve(0.5, ceiling=1.0)
        b.commit(rid1)
        b.rollback(rid2)
        assert b.get() == pytest.approx(0.3)

    def test_reserve_zero_raises_value_error(self, fake_redis_client):
        b = make_redis_backend(fake_redis_client)
        with pytest.raises(ValueError):
            b.reserve(0.0, ceiling=1.0)


class TestAdversarialRedisReserveConcurrent:
    def test_concurrent_reserve_ceiling_enforced(self, fake_redis_client):
        """10 threads attempt $0.15 each against $1.0 ceiling. At most 6 succeed."""
        b = make_redis_backend(fake_redis_client, chain_id="concurrent-reserve")
        successful = []
        lock = threading.Lock()

        def try_reserve():
            try:
                rid = b.reserve(0.15, ceiling=1.0)
                with lock:
                    successful.append(rid)
            except OverflowError:
                pass

        threads = [threading.Thread(target=try_reserve) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(successful) <= 6

    def test_redis_fallback_on_reserve_failure(self, fake_redis_client):
        """When Redis eval fails, reserve() falls back to LocalBudgetBackend."""
        b = make_redis_backend(fake_redis_client)
        # Simulate Redis failure by patching eval to raise
        original_eval = fake_redis_client.eval
        fake_redis_client.eval = MagicMock(side_effect=ConnectionError("Redis down"))
        try:
            rid = b.reserve(0.3, ceiling=1.0)
            assert rid is not None
            # Should be on fallback now
            assert b._using_fallback is True
        finally:
            fake_redis_client.eval = original_eval


# ---------------------------------------------------------------------------
# ExecutionContext: two-phase integration
# ---------------------------------------------------------------------------


class TestExecutionContextReserveIntegration:
    def test_wrap_uses_reserve_when_estimate_provided(self):
        """With cost_estimate_hint, _wrap() calls reserve() before fn()."""
        backend = LocalBudgetBackend()
        called_reserve = []
        original_reserve = backend.reserve

        def tracking_reserve(amount, ceiling):
            called_reserve.append(amount)
            return original_reserve(amount, ceiling)

        backend.reserve = tracking_reserve

        config = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=3)
        config = ExecutionConfig.__class__.__call__(
            ExecutionConfig,
            max_cost_usd=1.0,
            max_steps=10,
            max_retries_total=3,
            budget_backend=backend,
        )
        ctx = ExecutionContext(config=config)
        ctx._budget_backend = backend

        result = ctx.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(cost_estimate_hint=0.1),
        )
        assert result == Decision.ALLOW
        assert len(called_reserve) == 1
        assert called_reserve[0] == pytest.approx(0.1)

    def test_wrap_rollback_on_fn_exception(self):
        """If fn() raises, the reservation is rolled back."""
        backend = LocalBudgetBackend()
        config = ExecutionConfig(
            max_cost_usd=1.0,
            max_steps=10,
            max_retries_total=3,
            budget_backend=backend,
        )
        ctx = ExecutionContext(config=config)
        ctx._budget_backend = backend

        def failing_fn():
            raise ValueError("boom")

        result = ctx.wrap_llm_call(
            fn=failing_fn,
            options=WrapOptions(cost_estimate_hint=0.1),
        )
        assert result == Decision.RETRY
        # After rollback, no cost accumulated and no reservation held
        assert backend.get() == 0.0
        assert backend.get_reserved() == 0.0

    def test_wrap_ceiling_enforced_via_reserve(self):
        """When reserve() raises OverflowError, wrap returns HALT."""
        backend = LocalBudgetBackend()
        backend.add(0.95)  # Already spent 95 cents
        config = ExecutionConfig(
            max_cost_usd=1.0,
            max_steps=10,
            max_retries_total=3,
            budget_backend=backend,
        )
        ctx = ExecutionContext(config=config)
        ctx._budget_backend = backend

        result = ctx.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(cost_estimate_hint=0.1),
        )
        assert result == Decision.HALT

    def test_wrap_backward_compat_without_reserve(self):
        """Backend without reserve() method uses legacy projection check."""

        class LegacyBackend:
            def __init__(self):
                self._cost = 0.0
                self._lock = threading.Lock()

            def add(self, amount):
                with self._lock:
                    self._cost += amount
                    return self._cost

            def get(self):
                with self._lock:
                    return self._cost

            def reset(self):
                with self._lock:
                    self._cost = 0.0

            def close(self):
                pass

        backend = LegacyBackend()
        config = ExecutionConfig(
            max_cost_usd=1.0,
            max_steps=10,
            max_retries_total=3,
            budget_backend=backend,
        )
        ctx = ExecutionContext(config=config)
        ctx._budget_backend = backend

        result = ctx.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(cost_estimate_hint=0.1),
        )
        assert result == Decision.ALLOW
        assert backend.get() == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Item 1c: CancellationToken in _propagate_child_cost
# ---------------------------------------------------------------------------


class TestCancellationTokenPropagation:
    def test_cancellation_token_signalled_when_child_exceeds_budget(self):
        """After child cost pushes parent over ceiling, token is cancelled."""
        parent_cfg = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=3)
        parent = ExecutionContext(config=parent_cfg)

        # Manually accumulate some cost so that child propagation will exceed ceiling
        with parent._lock:
            parent._cost_usd_accumulated = 0.9

        assert not parent._cancellation_token.is_cancelled

        parent._propagate_child_cost(0.15)

        assert parent._aborted is True
        assert parent._cancellation_token.is_cancelled

    def test_cancellation_token_not_signalled_when_within_budget(self):
        """Child cost within budget does not cancel token."""
        parent_cfg = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=3)
        parent = ExecutionContext(config=parent_cfg)

        parent._propagate_child_cost(0.3)

        assert parent._aborted is False
        assert not parent._cancellation_token.is_cancelled

    def test_subsequent_wrap_halts_after_child_budget_exceeded(self):
        """After child exceeds parent budget (token cancelled), new wraps return HALT."""
        parent_cfg = ExecutionConfig(max_cost_usd=0.5, max_steps=10, max_retries_total=3)
        parent = ExecutionContext(config=parent_cfg)

        # Simulate child cost that blows the ceiling
        parent._propagate_child_cost(0.6)

        assert parent._cancellation_token.is_cancelled

        result = parent.wrap_llm_call(fn=lambda: None)
        assert result == Decision.HALT

    def test_cancellation_propagates_up_chain(self):
        """Cancellation token is set on all ancestors when deepest child overflows."""
        grandparent_cfg = ExecutionConfig(max_cost_usd=2.0, max_steps=10, max_retries_total=3)
        grandparent = ExecutionContext(config=grandparent_cfg)

        parent_cfg = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=3)
        parent = ExecutionContext(config=parent_cfg, parent=grandparent)

        # Accumulate near ceiling on grandparent
        with grandparent._lock:
            grandparent._cost_usd_accumulated = 1.9

        # Child cost propagates: parent overflows, then grandparent overflows too
        parent._propagate_child_cost(1.1)

        assert parent._aborted is True
        assert parent._cancellation_token.is_cancelled
        assert grandparent._aborted is True
        assert grandparent._cancellation_token.is_cancelled
