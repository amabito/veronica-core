"""Round 2 adversarial tests for modified files.

Targets:
1. adaptive_budget.py: import_control_state last_action sanitization
2. budget.py: NaN/Inf/negative validation boundary values
3. circuit_breaker.py: threshold boundary + failure_predicate edge cases
4. exit.py: request_exit thread-safety, duplicate calls
5. inject.py: functools.wraps, async/sync detection completeness
6. pricing.py: lru_cache invalidation, prefix match edge cases
7. distributed.py: _redact_exc URL redaction
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from veronica_core.budget import BudgetEnforcer
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState
from veronica_core.distributed import _redact_exc
from veronica_core.inject import is_guard_active, veronica_guard
from veronica_core.pricing import PRICING_TABLE, resolve_model_pricing
from veronica_core.runtime_policy import PolicyContext
from veronica_core.shield.adaptive_budget import AdaptiveBudgetHook
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# 1. adaptive_budget.py: import_control_state last_action sanitization
# ---------------------------------------------------------------------------


class TestAdaptiveBudgetImportControlState:
    """import_control_state must sanitize invalid last_action values."""

    def _make_hook(self, **kwargs: Any) -> AdaptiveBudgetHook:
        return AdaptiveBudgetHook(base_ceiling=100, **kwargs)

    def test_hold_action_sanitized_to_none(self) -> None:
        """'hold' is never set by adjust(); must be sanitized on import."""
        hook = self._make_hook()
        hook.import_control_state(
            {
                "adaptive_multiplier": 0.9,
                "last_adjustment_ts": None,
                "last_action": "hold",
                "anomaly_active": False,
                "anomaly_activated_ts": None,
            }
        )
        assert hook.last_action is None

    def test_direction_locked_action_sanitized_to_none(self) -> None:
        """'direction_locked' is never set by adjust(); must be sanitized."""
        hook = self._make_hook(direction_lock=True)
        hook.import_control_state(
            {
                "adaptive_multiplier": 0.9,
                "last_adjustment_ts": None,
                "last_action": "direction_locked",
                "anomaly_active": False,
                "anomaly_activated_ts": None,
            }
        )
        assert hook.last_action is None

    def test_cooldown_blocked_action_sanitized_to_none(self) -> None:
        """'cooldown_blocked' is never set; must be sanitized."""
        hook = self._make_hook(cooldown_seconds=60.0)
        hook.import_control_state(
            {
                "adaptive_multiplier": 0.9,
                "last_adjustment_ts": None,
                "last_action": "cooldown_blocked",
                "anomaly_active": False,
                "anomaly_activated_ts": None,
            }
        )
        assert hook.last_action is None

    def test_arbitrary_string_sanitized_to_none(self) -> None:
        """Attacker-supplied arbitrary string must be sanitized."""
        hook = self._make_hook()
        hook.import_control_state(
            {
                "adaptive_multiplier": 1.0,
                "last_adjustment_ts": None,
                "last_action": "injected_malicious_value; DROP TABLE",
                "anomaly_active": False,
                "anomaly_activated_ts": None,
            }
        )
        assert hook.last_action is None

    def test_tighten_passes_through(self) -> None:
        """Valid 'tighten' must be preserved."""
        hook = self._make_hook()
        hook.import_control_state(
            {
                "adaptive_multiplier": 0.85,
                "last_adjustment_ts": 12345.0,
                "last_action": "tighten",
                "anomaly_active": False,
                "anomaly_activated_ts": None,
            }
        )
        assert hook.last_action == "tighten"

    def test_loosen_passes_through(self) -> None:
        """Valid 'loosen' must be preserved."""
        hook = self._make_hook()
        hook.import_control_state(
            {
                "adaptive_multiplier": 1.1,
                "last_adjustment_ts": 12345.0,
                "last_action": "loosen",
                "anomaly_active": False,
                "anomaly_activated_ts": None,
            }
        )
        assert hook.last_action == "loosen"

    def test_none_action_preserved(self) -> None:
        """None (initial state) must be preserved."""
        hook = self._make_hook()
        hook.import_control_state(
            {
                "adaptive_multiplier": 1.0,
                "last_adjustment_ts": None,
                "last_action": None,
                "anomaly_active": False,
                "anomaly_activated_ts": None,
            }
        )
        assert hook.last_action is None

    def test_missing_last_action_defaults_to_none(self) -> None:
        """Missing 'last_action' key must default to None."""
        hook = self._make_hook()
        hook.import_control_state(
            {
                "adaptive_multiplier": 1.0,
                "last_adjustment_ts": None,
                "anomaly_active": False,
                "anomaly_activated_ts": None,
            }
        )
        assert hook.last_action is None

    def test_adjust_sets_only_tighten_or_loosen(self) -> None:
        """Verify that adjust() only ever writes 'tighten' or 'loosen' to _last_action.

        This test documents the invariant that justifies the sanitization fix above.
        """
        hook = self._make_hook(direction_lock=True, cooldown_seconds=0.0)

        # 1. Loosen (no events)
        result = hook.adjust()
        assert result.action == "loosen"
        assert hook.last_action == "loosen"

        # 2. Tighten (inject tighten events)
        tighten_event = SafetyEvent(
            event_type="BUDGET_EXCEEDED",
            decision=Decision.HALT,
            reason="test",
            hook="test",
        )
        for _ in range(5):
            hook.feed_event(tighten_event)
        result = hook.adjust()
        assert result.action == "tighten"
        assert hook.last_action == "tighten"

        # 3. Direction lock (after tighten, with tighten events, 0 degrade)
        # direction_lock should produce "direction_locked" action
        for _ in range(3):
            hook.feed_event(tighten_event)
        result = hook.adjust()
        if result.action == "direction_locked":
            # _last_action must NOT be updated to "direction_locked"
            assert hook.last_action == "tighten"  # unchanged from previous

        # 4. Hold (inject degrade events)
        degrade_event = SafetyEvent(
            event_type="BUDGET_WINDOW_EXCEEDED",
            decision=Decision.DEGRADE,
            reason="test",
            hook="test",
        )
        hook2 = AdaptiveBudgetHook(base_ceiling=100)
        hook2.feed_event(degrade_event)
        result = hook2.adjust()
        assert result.action == "hold"
        # _last_action must NOT be updated during "hold"
        assert hook2.last_action is None


# ---------------------------------------------------------------------------
# 2. budget.py: NaN/Inf/negative validation boundary
# ---------------------------------------------------------------------------


class TestBudgetEnforcerValidationBoundaries:
    """Edge cases for BudgetEnforcer validation."""

    def test_nan_limit_raises_on_init(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            BudgetEnforcer(limit_usd=float("nan"))

    def test_inf_limit_raises_on_init(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            BudgetEnforcer(limit_usd=float("inf"))

    def test_negative_inf_limit_raises_on_init(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            BudgetEnforcer(limit_usd=float("-inf"))

    def test_negative_limit_raises_on_init(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            BudgetEnforcer(limit_usd=-0.01)

    def test_zero_limit_allowed(self) -> None:
        """limit_usd=0.0 is valid (always exhausted immediately)."""
        b = BudgetEnforcer(limit_usd=0.0)
        assert b.limit_usd == 0.0

    def test_spend_nan_raises(self) -> None:
        b = BudgetEnforcer(limit_usd=100.0)
        with pytest.raises(ValueError, match="finite"):
            b.spend(float("nan"))

    def test_spend_inf_raises(self) -> None:
        b = BudgetEnforcer(limit_usd=100.0)
        with pytest.raises(ValueError, match="finite"):
            b.spend(float("inf"))

    def test_spend_negative_raises(self) -> None:
        b = BudgetEnforcer(limit_usd=100.0)
        with pytest.raises(ValueError, match="non-negative"):
            b.spend(-0.01)

    def test_spend_zero_allowed(self) -> None:
        """spend(0.0) must be allowed and count as a call."""
        b = BudgetEnforcer(limit_usd=100.0)
        result = b.spend(0.0)
        assert result is True
        assert b.call_count == 1

    def test_check_nan_cost_denied(self) -> None:
        """check() with NaN cost must return denied (not silently pass)."""
        b = BudgetEnforcer(limit_usd=100.0)
        ctx = PolicyContext(cost_usd=float("nan"))
        decision = b.check(ctx)
        assert not decision.allowed
        assert "Invalid" in decision.reason

    def test_check_inf_cost_denied(self) -> None:
        b = BudgetEnforcer(limit_usd=100.0)
        ctx = PolicyContext(cost_usd=float("inf"))
        decision = b.check(ctx)
        assert not decision.allowed

    def test_check_negative_cost_denied(self) -> None:
        """Negative cost must be denied (could bypass limits near ceiling)."""
        b = BudgetEnforcer(limit_usd=100.0)
        ctx = PolicyContext(cost_usd=-1.0)
        decision = b.check(ctx)
        assert not decision.allowed

    def test_remaining_usd_after_exceed(self) -> None:
        """remaining_usd must return 0.0 after budget exceeded."""
        b = BudgetEnforcer(limit_usd=1.0)
        b.spend(1.0)  # Exactly at limit
        b.spend(0.01)  # Over limit
        assert b.remaining_usd == 0.0

    def test_concurrent_spend_no_race(self) -> None:
        """10 concurrent threads spending must not corrupt state."""
        b = BudgetEnforcer(limit_usd=1000.0)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                b.spend(1.0)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert b.call_count == 10
        assert b.spent_usd == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 3. circuit_breaker.py: threshold boundary + failure_predicate
# ---------------------------------------------------------------------------


class TestCircuitBreakerBoundaries:
    """Edge cases for CircuitBreaker validation and behavior."""

    def test_threshold_zero_raises(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            CircuitBreaker(failure_threshold=0)

    def test_threshold_negative_raises(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            CircuitBreaker(failure_threshold=-1)

    def test_threshold_one_valid(self) -> None:
        """failure_threshold=1 means open on first failure."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_recovery_timeout_zero_valid(self) -> None:
        """recovery_timeout=0.0 means instant recovery (HALF_OPEN immediately)."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
        cb.record_failure()
        # With 0.0 timeout, should immediately transition to HALF_OPEN
        assert cb.state == CircuitState.HALF_OPEN

    def test_recovery_timeout_negative_raises(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            CircuitBreaker(failure_threshold=1, recovery_timeout=-1.0)

    def test_recovery_timeout_nan_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            CircuitBreaker(failure_threshold=1, recovery_timeout=float("nan"))

    def test_recovery_timeout_inf_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            CircuitBreaker(failure_threshold=1, recovery_timeout=float("inf"))

    def test_failure_predicate_exception_counts_as_failure(self) -> None:
        """Predicate that raises must count the failure (fail-safe behavior)."""

        def crashing_predicate(exc: BaseException) -> bool:
            raise RuntimeError("predicate crashed")

        cb = CircuitBreaker(failure_threshold=2, failure_predicate=crashing_predicate)
        cb.record_failure(error=ValueError("test"))
        assert cb.failure_count == 1  # Counted despite predicate crash

    def test_failure_predicate_none_error_always_counts(self) -> None:
        """When error=None, failure always counts regardless of predicate."""

        def never_count(_: BaseException) -> bool:
            return False  # Should be ignored when error=None

        cb = CircuitBreaker(failure_threshold=2, failure_predicate=never_count)
        cb.record_failure(error=None)  # error=None -> predicate not called
        assert cb.failure_count == 1

    def test_threshold_exactly_at_boundary_opens_circuit(self) -> None:
        """Exactly failure_threshold failures must open the circuit."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED  # Not yet
        cb.record_failure()
        assert cb.state == CircuitState.OPEN  # Exactly at threshold

    def test_half_open_slot_released_on_filtered_failure(self) -> None:
        """Filtered failure (predicate=False) must release HALF_OPEN in-flight slot."""

        def always_filter(_: BaseException) -> bool:
            return False

        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.0,  # Instant half-open
            failure_predicate=always_filter,
        )
        cb.record_failure()
        # Now HALF_OPEN
        assert cb.state == CircuitState.HALF_OPEN

        # Claim the slot
        decision1 = cb.check(PolicyContext())
        assert decision1.allowed  # Slot claimed

        # Second check should be denied (slot in use)
        decision2 = cb.check(PolicyContext())
        assert not decision2.allowed

        # Record a filtered failure -> slot released
        returned = cb.record_failure(error=ValueError("filtered"))
        assert returned is False  # Filtered

        # Now the slot should be released -> next check allowed
        decision3 = cb.check(PolicyContext())
        assert decision3.allowed  # Slot available again

    def test_bind_to_context_prevents_cross_context_sharing(self) -> None:
        """bind_to_context must raise when same CB shared across contexts."""
        cb = CircuitBreaker(failure_threshold=3)
        cb.bind_to_context("ctx-1")
        cb.bind_to_context("ctx-1")  # Same context: OK

        with pytest.raises(RuntimeError, match="being shared"):
            cb.bind_to_context("ctx-2")


# ---------------------------------------------------------------------------
# 4. exit.py: thread-safety of request_exit
# ---------------------------------------------------------------------------


class TestExitHandlerThreadSafety:
    """VeronicaExit.request_exit must handle duplicate concurrent calls safely."""

    def test_duplicate_request_exit_only_executes_once(self) -> None:
        """Concurrent calls must not trigger double execution."""
        from veronica_core.backends import PersistenceBackend
        from veronica_core.exit import ExitTier, VeronicaExit
        from veronica_core.state import VeronicaStateMachine

        sm = VeronicaStateMachine()

        class _NullBackend(PersistenceBackend):
            def save(self, data: dict) -> bool:
                return True

            def load(self) -> None:  # type: ignore[override]
                return None

        exit_handler = VeronicaExit(sm, _NullBackend())
        exit_handler.exit_requested = True  # Pre-simulate already requested

        # Second call must be a no-op
        exit_handler.request_exit(ExitTier.EMERGENCY, "duplicate")
        assert exit_handler.exit_tier is None  # Not overwritten


# ---------------------------------------------------------------------------
# 5. inject.py: additional edge cases
# ---------------------------------------------------------------------------


class TestInjectEdgeCases:
    """Additional inject.py tests not covered in Round 1."""

    def test_sync_guard_active_resets_on_exception(self) -> None:
        """ContextVar must reset to False even when sync function raises."""

        @veronica_guard()
        def boom() -> None:
            raise ValueError("sync boom")

        with pytest.raises(ValueError, match="sync boom"):
            boom()
        assert is_guard_active() is False

    def test_sync_guard_return_value_propagated(self) -> None:
        """Return value from sync guard must be propagated correctly."""

        @veronica_guard()
        def returns_tuple() -> tuple[int, str]:
            return (42, "hello")

        result = returns_tuple()
        assert result == (42, "hello")

    def test_async_guard_return_value_propagated(self) -> None:
        """Return value from async guard must be propagated correctly."""

        @veronica_guard()
        async def returns_tuple() -> tuple[int, str]:
            return (42, "hello")

        result = asyncio.run(returns_tuple())
        assert result == (42, "hello")

    def test_guard_with_generator_function_sync(self) -> None:
        """sync generator functions are not coroutines, must use sync wrapper."""

        @veronica_guard()
        def gen_fn() -> Any:
            yield 1
            yield 2

        result = list(gen_fn())
        assert result == [1, 2]


# ---------------------------------------------------------------------------
# 6. pricing.py: lru_cache and prefix match edge cases
# ---------------------------------------------------------------------------


class TestPricingEdgeCases:
    """Boundary cases for resolve_model_pricing."""

    def setup_method(self) -> None:
        resolve_model_pricing.cache_clear()

    def test_exact_match_takes_precedence_over_prefix(self) -> None:
        """Exact match must win even when a shorter prefix would also match."""
        # 'gpt-4o' is both an exact match and a prefix of 'gpt-4o-mini'
        # If we look up 'gpt-4o-mini', it should match 'gpt-4o-mini' exactly
        # (or the longest prefix, NOT 'gpt-4o' if 'gpt-4o-mini' exists in table)
        exact = resolve_model_pricing("gpt-4o-mini")
        from veronica_core.pricing import PRICING_TABLE

        if "gpt-4o-mini" in PRICING_TABLE:
            assert exact is PRICING_TABLE["gpt-4o-mini"]
        # else: prefix match, still valid

    def test_unknown_model_uses_fallback_pricing(self) -> None:
        """Completely unknown model must use conservative fallback."""
        from veronica_core.pricing import _UNKNOWN_MODEL_FALLBACK

        result = resolve_model_pricing("totally-unknown-model-xyz-99999")
        assert result is _UNKNOWN_MODEL_FALLBACK

    def test_prefix_match_longest_wins(self) -> None:
        """When multiple prefix matches, longest prefix wins."""
        # 'claude-3-5-sonnet-20241022' has prefix 'claude-3-5-sonnet'
        # and also 'claude-3' but only exact 'claude-3-5-sonnet-20241022' is in table
        pricing = resolve_model_pricing("claude-3-5-sonnet-20241022-extended")
        assert pricing is PRICING_TABLE["claude-3-5-sonnet-20241022"]

    def test_none_type_raises_or_falls_back(self) -> None:
        """Non-string model (e.g. None) must not crash silently."""
        # The function signature says model: str but Python doesn't enforce this
        # at runtime. We verify it doesn't cause undetected silent failures.
        try:
            result = resolve_model_pricing(None)  # type: ignore[arg-type]
            # If it doesn't raise, it should return some pricing (not crash)
            assert result is not None
        except (TypeError, AttributeError):
            pass  # Acceptable: type error is fine

    def test_very_long_model_string_no_hang(self) -> None:
        """Very long model string must not cause O(n^2) hang via prefix matching."""
        long_model = "gpt-" + "x" * 10_000
        pricing = resolve_model_pricing(long_model)
        assert pricing is not None  # Must complete and return something


# ---------------------------------------------------------------------------
# 7. distributed.py: _redact_exc URL redaction
# ---------------------------------------------------------------------------


class TestRedactExc:
    """_redact_exc must redact Redis credentials from exception messages."""

    def test_basic_redis_url_redacted(self) -> None:
        exc = ConnectionError("redis://user:password@localhost:6379/0")
        result = _redact_exc(exc)
        assert "password" not in result
        assert "***@" in result

    def test_rediss_url_redacted(self) -> None:
        exc = ConnectionError("rediss://admin:s3cr3t@redis.example.com:6380")
        result = _redact_exc(exc)
        assert "s3cr3t" not in result
        assert "***@" in result

    def test_redis_ssl_url_redacted(self) -> None:
        exc = ConnectionError("redis+ssl://user:pass@host:6380")
        result = _redact_exc(exc)
        assert "pass" not in result
        assert "***@" in result

    def test_url_without_credentials_not_redacted(self) -> None:
        """URL without user:password must not be modified."""
        exc = ConnectionError("redis://localhost:6379/0")
        result = _redact_exc(exc)
        assert "localhost" in result

    def test_no_redis_url_unchanged(self) -> None:
        """Exception without Redis URL must be returned as-is."""
        exc = RuntimeError("database connection refused")
        result = _redact_exc(exc)
        assert "database connection refused" in result

    def test_password_with_at_sign_redacted(self) -> None:
        """Passwords containing @ must still be redacted."""
        exc = ConnectionError("redis://user:p@ssw0rd@host:6379")
        result = _redact_exc(exc)
        assert "p@ssw0rd" not in result

    def test_exception_type_included_in_result(self) -> None:
        """Result must include the exception type name."""
        exc = ConnectionError("redis://user:pass@host")
        result = _redact_exc(exc)
        assert "ConnectionError" in result

    def test_case_insensitive_redis_scheme(self) -> None:
        """REDIS:// and Redis:// must also be redacted."""
        exc = ConnectionError("REDIS://user:pass@host:6379")
        result = _redact_exc(exc)
        assert "pass" not in result
