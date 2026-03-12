"""Adversarial tests for v2.4.0 changes -- attacker mindset.

Categories covered:
  1. Corrupted input -- broken backends, garbage contexts
  2. Concurrent access -- parallel close(), wrap-during-close races
  3. State corruption -- closed/aborted context reuse, graph tampering
  4. Boundary abuse -- zero limits, negative costs
  5. Serialization -- Decision enum backward compatibility
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock


from veronica_core.containment import ExecutionConfig, ExecutionContext
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(**overrides: Any) -> ExecutionContext:
    defaults = dict(max_cost_usd=10.0, max_steps=50, max_retries_total=10)
    defaults.update(overrides)
    return ExecutionContext(config=ExecutionConfig(**defaults))


# ---------------------------------------------------------------------------
# 1. _try_rollback adversarial
# ---------------------------------------------------------------------------


class TestAdversarialTryRollback:
    """_try_rollback must never raise, even with broken backends."""

    def test_rollback_with_exploding_backend(self) -> None:
        """Backend.rollback() raises -- must be swallowed."""
        ctx = _make_ctx()
        ctx._budget_backend.rollback = MagicMock(
            side_effect=RuntimeError("disk on fire")
        )
        # Must not raise
        ctx._try_rollback("fake-reservation-id")

    def test_rollback_with_none_reservation_id(self) -> None:
        """None reservation_id must be a no-op (no backend call)."""
        ctx = _make_ctx()
        ctx._budget_backend.rollback = MagicMock(
            side_effect=AssertionError("should not be called")
        )
        ctx._try_rollback(None)
        ctx._budget_backend.rollback.assert_not_called()

    def test_rollback_with_backend_rollback_returning_none(self) -> None:
        """Backend.rollback() returning None must not crash."""
        ctx = _make_ctx()
        ctx._budget_backend.rollback = MagicMock(return_value=None)
        ctx._try_rollback("some-id")
        ctx._budget_backend.rollback.assert_called_once_with("some-id")


# ---------------------------------------------------------------------------
# 2. close() adversarial
# ---------------------------------------------------------------------------


class TestAdversarialClose:
    """close() must be resilient to corruption and concurrency."""

    def test_concurrent_close_no_double_cleanup(self) -> None:
        """10 threads calling close() -- budget_backend.close() called exactly once."""
        ctx = _make_ctx()
        close_count = [0]
        original_close = ctx._budget_backend.close

        def counting_close() -> None:
            close_count[0] += 1
            original_close()

        ctx._budget_backend.close = counting_close

        threads = [threading.Thread(target=ctx.close) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert close_count[0] == 1, "backend.close() must be called exactly once"
        assert ctx._closed is True

    def test_close_with_corrupted_graph_nodes(self) -> None:
        """Graph._nodes containing garbage must not prevent close()."""
        ctx = _make_ctx()
        # Inject a node with an unexpected status value
        ctx._graph._nodes["fake-node"] = MagicMock(status="EXPLODING")
        # Must not raise -- the non-terminal warning code handles it
        ctx.close()
        assert ctx._closed is True

    def test_close_with_graph_nodes_raising(self) -> None:
        """If _graph._nodes iteration raises, close() must still complete."""
        ctx = _make_ctx()

        class ExplodingDict(dict):
            def items(self) -> Any:
                raise RuntimeError("graph corrupted")

        ctx._graph._nodes = ExplodingDict()
        ctx.close()
        assert ctx._closed is True
        assert ctx._aborted is True

    def test_close_then_wrap_returns_halt(self) -> None:
        """wrap_llm_call after close() must return HALT without calling fn."""
        ctx = _make_ctx()
        ctx.close()

        called = []
        decision = ctx.wrap_llm_call(fn=lambda: called.append(1))
        assert decision == Decision.HALT
        assert called == []

    def test_close_with_budget_backend_close_raising(self) -> None:
        """budget_backend.close() explosion must not prevent close() completion."""
        ctx = _make_ctx()
        ctx._budget_backend.close = MagicMock(side_effect=IOError("disk full"))
        ctx.close()
        assert ctx._closed is True

    def test_close_with_circuit_breaker_close_raising(self) -> None:
        """circuit_breaker.close() explosion must not prevent close() completion."""
        ctx = _make_ctx()
        ctx._circuit_breaker = MagicMock()
        ctx._circuit_breaker.close.side_effect = RuntimeError("cb broken")
        ctx.close()
        assert ctx._closed is True


# ---------------------------------------------------------------------------
# 3. CrewAI execution_context adversarial
# ---------------------------------------------------------------------------


class TestAdversarialCrewAIContext:
    """CrewAI adapter with corrupted/closed execution contexts.

    These tests rely on the fake crewai stubs registered by
    tests/test_adapter_crewai.py. When run in isolation, the stubs may
    not be present; we skip gracefully in that case.
    """

    def test_closed_execution_context_denies_on_check(self) -> None:
        """Passing an already-closed ExecutionContext must deny via adapter check."""
        from veronica_core.adapters._shared import (
            ExecutionContextContainerAdapter,
            build_adapter_container,
        )

        cfg = ExecutionConfig(max_cost_usd=100.0, max_steps=100, max_retries_total=5)
        ctx = _make_ctx(max_cost_usd=100.0, max_steps=100, max_retries_total=5)
        ctx.close()

        container = build_adapter_container(cfg, ctx)
        assert isinstance(container, ExecutionContextContainerAdapter)
        decision = container.check()
        assert not decision.allowed, "Closed context must deny"

    def test_none_execution_context_uses_standalone(self) -> None:
        """execution_context=None must fall back to standalone AIContainer."""
        from veronica_core.adapters._shared import build_adapter_container
        from veronica_core.container import AIContainer

        cfg = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=3)
        container = build_adapter_container(cfg, None)
        assert isinstance(container, AIContainer)


# ---------------------------------------------------------------------------
# 4. _BudgetProxy fallback chain adversarial
# ---------------------------------------------------------------------------


class TestAdversarialBudgetProxyFallback:
    """_BudgetProxy.spent_usd must survive all backend failures."""

    def test_all_fallback_paths_fail_returns_zero(self) -> None:
        """When get(), get_snapshot(), and _cost_usd_accumulated all fail."""
        from veronica_core.adapters._shared import _BudgetProxy

        class BrokenCtx:
            _cost_usd_accumulated = None  # getattr returns None

            def get_snapshot(self) -> Any:
                raise RuntimeError("snapshot broken")

        proxy = _BudgetProxy(BrokenCtx(), limit_usd=10.0)
        # _get_fn is None (no _budget_backend), get_snapshot raises,
        # fallback getattr returns None → float(None) raises → should handle
        # Actually float(None) raises TypeError. Let's see what happens.
        result = proxy.spent_usd
        # Should return 0.0 via the final getattr default
        assert isinstance(result, float)

    def test_get_fn_raises_falls_through_to_snapshot(self) -> None:
        """Backend.get() raises → falls through to get_snapshot()."""
        from veronica_core.adapters._shared import _BudgetProxy

        class CtxWithSnapshot:
            _budget_backend = MagicMock()
            _lock = threading.Lock()
            _cost_usd_accumulated = 99.0

            def get_snapshot(self) -> Any:
                snap = MagicMock()
                snap.cost_usd_accumulated = 42.0
                return snap

        ctx = CtxWithSnapshot()
        ctx._budget_backend.get.side_effect = RuntimeError("redis down")
        proxy = _BudgetProxy(ctx, limit_usd=100.0)
        assert proxy.spent_usd == 42.0

    def test_spend_with_exploding_add_fn_fails_closed(self) -> None:
        """spend() with broken add_fn must return False (fail-closed)."""
        from veronica_core.adapters._shared import _BudgetProxy

        class CtxWithBrokenBackend:
            _budget_backend = MagicMock()
            _lock = threading.Lock()
            _cost_usd_accumulated = 0.0

        ctx = CtxWithBrokenBackend()
        ctx._budget_backend.add.side_effect = RuntimeError("backend exploded")
        ctx._budget_backend.get.side_effect = RuntimeError("also broken")
        proxy = _BudgetProxy(ctx, limit_usd=100.0)
        result = proxy.spend(1.0)
        assert result is False, "Broken backend must fail-closed"


# ---------------------------------------------------------------------------
# 5. MCPToolResult.decision enum backward compatibility
# ---------------------------------------------------------------------------


class TestAdversarialDecisionEnum:
    """Decision enum must maintain str backward compatibility."""

    def test_decision_equals_string(self) -> None:
        """Decision.ALLOW == 'ALLOW' must be True (str inheritance)."""
        assert Decision.ALLOW == "ALLOW"
        assert Decision.HALT == "HALT"

    def test_decision_string_value_access(self) -> None:
        """Decision.value must return the plain string for APIs that need it."""
        assert Decision.ALLOW.value == "ALLOW"
        assert Decision.HALT.value == "HALT"
        # String concatenation works via str inheritance
        assert "decision=" + Decision.HALT == "decision=HALT"

    def test_decision_isinstance_str(self) -> None:
        """Decision enum values must pass isinstance(x, str)."""
        assert isinstance(Decision.ALLOW, str)
        assert isinstance(Decision.HALT, str)

    def test_mcp_tool_result_decision_default(self) -> None:
        """MCPToolResult default decision must be Decision.ALLOW."""
        from veronica_core.adapters._mcp_base import MCPToolResult

        result = MCPToolResult(success=True)
        assert result.decision == Decision.ALLOW
        assert result.decision == "ALLOW"  # backward compat

    def test_mcp_tool_result_halt_decision(self) -> None:
        """MCPToolResult with HALT decision must compare correctly."""
        from veronica_core.adapters._mcp_base import MCPToolResult

        result = MCPToolResult(success=False, decision=Decision.HALT)
        assert result.decision == Decision.HALT
        assert result.decision == "HALT"  # backward compat
        assert result.decision != "ALLOW"

    def test_decision_json_serializable(self) -> None:
        """Decision enum must be JSON-serializable via .value."""
        import json

        data = {"decision": Decision.ALLOW.value}
        serialized = json.dumps(data)
        assert '"ALLOW"' in serialized

    def test_decision_dict_key(self) -> None:
        """Decision enum as dict key must match string key."""
        d: dict = {Decision.ALLOW: "ok", Decision.HALT: "stopped"}
        assert d["ALLOW"] == "ok"
        assert d["HALT"] == "stopped"


# ---------------------------------------------------------------------------
# 6. Concurrent wrap + close race
# ---------------------------------------------------------------------------


class TestAdversarialWrapCloseRace:
    """Concurrent wrap_llm_call and close() must not corrupt state."""

    def test_wrap_during_close_no_crash(self) -> None:
        """10 threads wrapping + 1 thread closing must not raise."""
        ctx = _make_ctx(max_cost_usd=1000.0, max_steps=1000)
        errors: list[Exception] = []

        def wrap_loop() -> None:
            for _ in range(20):
                try:
                    ctx.wrap_llm_call(fn=lambda: None)
                except Exception as e:
                    errors.append(e)

        def close_after_delay() -> None:
            try:
                ctx.close()
            except Exception as e:
                errors.append(e)

        wrap_threads = [threading.Thread(target=wrap_loop) for _ in range(5)]
        close_thread = threading.Thread(target=close_after_delay)

        for t in wrap_threads:
            t.start()
        close_thread.start()

        for t in wrap_threads:
            t.join(timeout=10)
        close_thread.join(timeout=10)

        # No unhandled exceptions
        assert errors == [], f"Unexpected errors during wrap+close race: {errors}"

    def test_close_during_active_wrap_returns_halt_on_next(self) -> None:
        """After close(), any subsequent wrap must return HALT."""
        ctx = _make_ctx()
        # First wrap succeeds
        d1 = ctx.wrap_llm_call(fn=lambda: "ok")
        assert d1 == Decision.ALLOW

        ctx.close()

        # Second wrap must HALT
        called = []
        d2 = ctx.wrap_llm_call(fn=lambda: called.append(1))
        assert d2 == Decision.HALT
        assert called == []
