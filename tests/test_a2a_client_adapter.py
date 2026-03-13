"""Tests for veronica_core.adapters.a2a_client.

Covers BoundA2AAdapter, A2AClientContainmentAdapter, wrap_a2a_agent.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from veronica_core.adapters._a2a_base import A2AClientConfig, A2AMessageCost, A2AResult
from veronica_core.adapters.a2a_client import (
    A2AClientContainmentAdapter,
    BoundA2AAdapter,
    wrap_a2a_agent,
)
from veronica_core.a2a.provenance import A2AIdentityProvenance
from veronica_core.a2a.types import AgentIdentity, TrustLevel
from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.containment.execution_context import ExecutionContext
from veronica_core.containment.types import ExecutionConfig
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(max_cost: float = 100.0) -> ExecutionContext:
    return ExecutionContext(
        config=ExecutionConfig(max_cost_usd=max_cost, max_steps=1000, max_retries_total=10)
    )


def _make_identity(trust: TrustLevel = TrustLevel.TRUSTED) -> AgentIdentity:
    return AgentIdentity(agent_id="caller", origin="a2a", trust_level=trust)


def _make_client(response: Any = None, raises: Exception | None = None) -> MagicMock:
    """Minimal A2AClientProtocol stub."""
    client = MagicMock()
    if raises is not None:
        client.send_message = AsyncMock(side_effect=raises)
    else:
        client.send_message = AsyncMock(return_value=response or {"status": "ok"})
    return client


# ---------------------------------------------------------------------------
# A2AClientContainmentAdapter -- construction
# ---------------------------------------------------------------------------


class TestA2AClientContainmentAdapterConstruction:
    def test_none_ctx_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            A2AClientContainmentAdapter(execution_context=None)  # type: ignore[arg-type]

    def test_default_config_used_when_none(self) -> None:
        adapter = A2AClientContainmentAdapter(execution_context=_make_ctx())
        assert adapter._config.default_cost_per_message == 0.01

    def test_custom_config(self) -> None:
        cfg = A2AClientConfig(default_cost_per_message=0.05)
        adapter = A2AClientContainmentAdapter(execution_context=_make_ctx(), config=cfg)
        assert adapter._config.default_cost_per_message == 0.05


# ---------------------------------------------------------------------------
# Happy path: send_message
# ---------------------------------------------------------------------------


class TestSendMessageHappyPath:
    def test_successful_call_returns_allow(self) -> None:
        async def _run() -> None:
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx())
            client = _make_client({"status": "ok"})
            result = await adapter.send_message(
                agent_id="agent-1",
                message={"text": "hello"},
                tenant_id="t1",
                identity=_make_identity(),
                client=client,
            )
            assert result.success is True
            assert result.decision == Decision.ALLOW
            client.send_message.assert_called_once()

        asyncio.run(_run())

    def test_cost_tracked_in_stats(self) -> None:
        async def _run() -> None:
            costs = {"agent-1": A2AMessageCost("agent-1", cost_per_message=0.05)}
            adapter = A2AClientContainmentAdapter(
                execution_context=_make_ctx(),
                message_costs=costs,
            )
            client = _make_client()
            await adapter.send_message(
                agent_id="agent-1",
                message={},
                tenant_id="t1",
                identity=_make_identity(),
                client=client,
            )
            all_stats = adapter.get_stats()
            assert "agent-1" in all_stats
            assert all_stats["agent-1"].message_count == 1

        asyncio.run(_run())

    def test_provenance_passthrough(self) -> None:
        """Provenance should not cause errors when passed."""

        async def _run() -> None:
            prov = A2AIdentityProvenance(card_verified=True, card_fingerprint="abc123")
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx())
            client = _make_client()
            result = await adapter.send_message(
                agent_id="agent-1",
                message={},
                tenant_id="t1",
                identity=_make_identity(),
                provenance=prov,
                client=client,
            )
            assert result.success is True

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_open_circuit_returns_halt(self) -> None:
        async def _run() -> None:
            cb = CircuitBreaker(failure_threshold=1)
            # Trip the circuit breaker
            cb.record_failure(error=RuntimeError("fail"))
            adapter = A2AClientContainmentAdapter(
                execution_context=_make_ctx(),
                circuit_breaker=cb,
            )
            client = _make_client()
            result = await adapter.send_message(
                agent_id="agent-1",
                message={},
                tenant_id="t1",
                identity=_make_identity(),
                client=client,
            )
            assert result.decision == Decision.HALT
            assert result.success is False
            # Client must not be called when CB is open
            client.send_message.assert_not_called()

        asyncio.run(_run())

    def test_closed_circuit_allows_call(self) -> None:
        async def _run() -> None:
            cb = CircuitBreaker(failure_threshold=10)
            adapter = A2AClientContainmentAdapter(
                execution_context=_make_ctx(),
                circuit_breaker=cb,
            )
            client = _make_client()
            result = await adapter.send_message(
                agent_id="agent-1",
                message={},
                tenant_id="t1",
                identity=_make_identity(),
                client=client,
            )
            assert result.success is True
            client.send_message.assert_called_once()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class TestBudgetExceeded:
    def test_exhausted_budget_returns_halt(self) -> None:
        """Zero budget should block send_message."""

        async def _run() -> None:
            ctx = ExecutionContext(
                config=ExecutionConfig(max_cost_usd=0.0, max_steps=1000, max_retries_total=10)
            )
            adapter = A2AClientContainmentAdapter(execution_context=ctx)
            client = _make_client()
            result = await adapter.send_message(
                agent_id="agent-1",
                message={},
                tenant_id="t1",
                identity=_make_identity(),
                client=client,
            )
            assert result.decision == Decision.HALT
            assert result.success is False

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_slow_client_triggers_timeout(self) -> None:
        async def _run() -> None:
            async def slow_send(_msg: Any) -> Any:
                await asyncio.sleep(10.0)
                return {}

            client = MagicMock()
            client.send_message = slow_send
            cfg = A2AClientConfig(timeout_seconds=0.05)
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx(), config=cfg)
            result = await adapter.send_message(
                agent_id="agent-1",
                message={},
                tenant_id="t1",
                identity=_make_identity(),
                client=client,
            )
            assert result.success is False
            assert result.error is not None

        asyncio.run(_run())

    def test_error_message_does_not_leak_exception_type(self) -> None:
        """Rule 5: error must not contain exception class name."""

        async def _run() -> None:
            client = _make_client(raises=RuntimeError("internal details"))
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx())
            result = await adapter.send_message(
                agent_id="agent-1",
                message={},
                tenant_id="t1",
                identity=_make_identity(),
                client=client,
            )
            assert result.success is False
            assert result.error is not None
            assert "RuntimeError" not in result.error
            assert "internal details" not in result.error

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Stats cap
# ---------------------------------------------------------------------------


class TestStatsCap:
    def test_stats_cap_prevents_unbounded_growth(self) -> None:
        """stats_cap limits the number of tracked agent IDs."""

        async def _run() -> None:
            cfg = A2AClientConfig(stats_cap=3)
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx(), config=cfg)
            client = _make_client()
            for i in range(10):
                await adapter.send_message(
                    agent_id=f"agent-{i}",
                    message={},
                    tenant_id="t1",
                    identity=_make_identity(),
                    client=client,
                )
            all_stats = adapter.get_stats()
            assert len(all_stats) <= 3  # sequential calls: no race, exact cap

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Concurrent send_message
# ---------------------------------------------------------------------------


class TestConcurrentSendMessage:
    def test_concurrent_calls_all_succeed(self) -> None:
        async def _run() -> None:
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx(max_cost=500.0))
            client = _make_client()

            async def one_call(i: int) -> A2AResult:
                return await adapter.send_message(
                    agent_id="agent-1",
                    message={"seq": i},
                    tenant_id="t1",
                    identity=_make_identity(),
                    client=client,
                )

            results = await asyncio.gather(*[one_call(i) for i in range(10)])
            successful = [r for r in results if r.success]
            assert len(successful) == 10

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# wrap_a2a_agent factory
# ---------------------------------------------------------------------------


class TestWrapA2AAgent:
    def test_wrap_a2a_agent_creates_bound_adapter(self) -> None:
        async def _run() -> None:
            client = _make_client()
            adapter = wrap_a2a_agent(
                client=client,
                execution_context=_make_ctx(),
                config=A2AClientConfig(),
            )
            assert isinstance(adapter, BoundA2AAdapter)

        asyncio.run(_run())

    def test_bound_adapter_send_message(self) -> None:
        async def _run() -> None:
            client = _make_client()
            adapter = wrap_a2a_agent(
                client=client,
                execution_context=_make_ctx(),
            )
            result = await adapter.send_message(
                agent_id="agent-1",
                message={"text": "hi"},
                tenant_id="t1",
                identity=_make_identity(),
            )
            assert result.success is True

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# capabilities()
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_capabilities_output(self) -> None:
        adapter = A2AClientContainmentAdapter(execution_context=_make_ctx())
        caps = adapter.capabilities()
        assert caps.framework_name == "A2A"
        assert caps.supports_async is True

    def test_bound_adapter_capabilities(self) -> None:
        client = _make_client()
        adapter = wrap_a2a_agent(client=client, execution_context=_make_ctx())
        caps = adapter.capabilities()
        assert caps.framework_name == "A2A"


# ---------------------------------------------------------------------------
# Adversarial
# ---------------------------------------------------------------------------


class TestAdversarialClientAdapter:
    def test_empty_agent_id_raises(self) -> None:
        async def _run() -> None:
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx())
            client = _make_client()
            with pytest.raises(ValueError, match="agent_id"):
                await adapter.send_message(
                    agent_id="",
                    message={},
                    tenant_id="t1",
                    identity=_make_identity(),
                    client=client,
                )

        asyncio.run(_run())

    def test_empty_tenant_id_raises(self) -> None:
        async def _run() -> None:
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx())
            client = _make_client()
            with pytest.raises(ValueError, match="tenant_id"):
                await adapter.send_message(
                    agent_id="agent-1",
                    message={},
                    tenant_id="",
                    identity=_make_identity(),
                    client=client,
                )

        asyncio.run(_run())

    def test_client_raises_increments_error_count(self) -> None:
        async def _run() -> None:
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx())
            client = _make_client(raises=ConnectionError("network down"))
            result = await adapter.send_message(
                agent_id="agent-1",
                message={},
                tenant_id="t1",
                identity=_make_identity(),
                client=client,
            )
            assert result.success is False
            all_stats = adapter.get_stats()
            assert "agent-1" in all_stats
            assert all_stats["agent-1"].error_count >= 1

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Per-token cost delta reported to ExecutionContext (CRITICAL fix)
# ---------------------------------------------------------------------------


class TestPerTokenCostDelta:
    """Verify per-token cost is charged back to ExecutionContext budget."""

    def test_token_delta_charged_to_budget(self) -> None:
        """When cost_per_token > 0 and response has tokens, delta must be
        added to ExecutionContext._budget_backend and _limits."""
        async def _run() -> None:
            ctx = _make_ctx(max_cost=100.0)
            costs = {"agent-1": A2AMessageCost("agent-1", cost_per_message=0.01, cost_per_token=0.001)}
            adapter = A2AClientContainmentAdapter(
                execution_context=ctx, message_costs=costs,
            )
            # Client returns response with token_count
            client = _make_client({"status": "ok", "token_count": 100})
            result = await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            assert result.success is True
            # cost = 0.01 + 100 * 0.001 = 0.11
            assert result.cost_usd == pytest.approx(0.11, abs=0.001)
            # Budget backend should have been charged the delta
            snap = ctx.get_snapshot()
            assert snap.cost_usd_accumulated >= 0.11

        asyncio.run(_run())

    def test_no_token_delta_when_zero_per_token(self) -> None:
        """No delta charged when cost_per_token is 0 (default)."""
        async def _run() -> None:
            ctx = _make_ctx(max_cost=100.0)
            adapter = A2AClientContainmentAdapter(execution_context=ctx)
            client = _make_client({"status": "ok", "token_count": 500})
            result = await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            assert result.success is True
            assert result.cost_usd == pytest.approx(0.01, abs=0.001)

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# _extract_token_count adversarial inputs
# ---------------------------------------------------------------------------


class TestExtractTokenCount:
    """Adversarial tests for _extract_token_count helper."""

    def test_nested_usage_dict(self) -> None:
        from veronica_core.adapters.a2a_client import _extract_token_count
        assert _extract_token_count({"usage": {"total_tokens": 200}}) == 200

    def test_usage_dict_non_int(self) -> None:
        from veronica_core.adapters.a2a_client import _extract_token_count
        assert _extract_token_count({"usage": {"total_tokens": "not_int"}}) == 0

    def test_string_token_count(self) -> None:
        from veronica_core.adapters.a2a_client import _extract_token_count
        assert _extract_token_count({"token_count": "not_a_number"}) == 0

    def test_negative_token_count(self) -> None:
        from veronica_core.adapters.a2a_client import _extract_token_count
        assert _extract_token_count({"token_count": -5}) == 0

    def test_none_response(self) -> None:
        from veronica_core.adapters.a2a_client import _extract_token_count
        assert _extract_token_count(None) == 0

    def test_zero_token_count(self) -> None:
        from veronica_core.adapters.a2a_client import _extract_token_count
        assert _extract_token_count({"token_count": 0}) == 0

    def test_float_token_count_ignored(self) -> None:
        from veronica_core.adapters.a2a_client import _extract_token_count
        assert _extract_token_count({"token_count": 3.14}) == 0

    def test_bool_token_count_rejected(self) -> None:
        from veronica_core.adapters.a2a_client import _extract_token_count
        # bool is subclass of int but must be rejected (consistent with config guards)
        assert _extract_token_count({"token_count": True}) == 0
        assert _extract_token_count({"token_count": False}) == 0

    def test_attr_based_token_count(self) -> None:
        from veronica_core.adapters.a2a_client import _extract_token_count

        class Resp:
            token_count = 42

        assert _extract_token_count(Resp()) == 42

    def test_bool_attr_token_count_rejected(self) -> None:
        """Bool via attribute path must also be rejected (Rule 19 guard scope)."""
        from veronica_core.adapters.a2a_client import _extract_token_count

        class Resp:
            token_count = True  # type: ignore[assignment]

        assert _extract_token_count(Resp()) == 0

    @pytest.mark.parametrize("key", ["tokens", "total_tokens"])
    def test_flat_alternative_token_keys(self, key: str) -> None:
        """Flat 'tokens' and 'total_tokens' keys must be extracted."""
        from veronica_core.adapters.a2a_client import _extract_token_count

        assert _extract_token_count({key: 99}) == 99

    def test_token_count_key_takes_priority(self) -> None:
        """When multiple keys present, 'token_count' is checked first."""
        from veronica_core.adapters.a2a_client import _extract_token_count

        assert _extract_token_count({"token_count": 10, "tokens": 20}) == 10


# ---------------------------------------------------------------------------
# Task response branch (Unit-2 GAP 5)
# ---------------------------------------------------------------------------


class TestTaskResponseBranch:
    """Tests for _is_task detection in send_message success path."""

    def test_task_response_sets_task_field(self) -> None:
        """Response with .id and .status is classified as a task."""
        async def _run() -> None:
            class TaskResponse:
                id = "task-123"
                status = "completed"
                token_count = 5

            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx(max_cost=10.0))
            result = await adapter.send_message(
                agent_id="a1",
                message={"text": "hi"},
                tenant_id="t1",
                identity=_make_identity(),
                client=_make_client(response=TaskResponse()),
            )
            assert result.success is True
            assert result.task is not None
            assert result.task.id == "task-123"
            assert result.message is None

        asyncio.run(_run())

    def test_dict_response_sets_message_field(self) -> None:
        """Dict response (no .id/.status) is classified as a message."""
        async def _run() -> None:
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx(max_cost=10.0))
            result = await adapter.send_message(
                agent_id="a1",
                message={"text": "hi"},
                tenant_id="t1",
                identity=_make_identity(),
                client=_make_client(response={"reply": "hello", "token_count": 5}),
            )
            assert result.success is True
            assert result.message is not None
            assert result.task is None

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Stats cap guard-after (Rule 1)
# ---------------------------------------------------------------------------


class TestStatsCapGuardAfter:
    """Tests for behavior AFTER the stats cap is reached."""

    def test_send_message_succeeds_after_stats_cap(self) -> None:
        """Calls beyond stats cap still succeed (just not tracked)."""
        async def _run() -> None:
            cfg = A2AClientConfig(stats_cap=2)
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx(), config=cfg)
            client = _make_client()
            # Fill stats cap
            for i in range(3):
                await adapter.send_message(
                    agent_id=f"agent-{i}", message={}, tenant_id="t1",
                    identity=_make_identity(), client=client,
                )
            # 4th agent should still succeed
            result = await adapter.send_message(
                agent_id="agent-99", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            assert result.success is True
            assert "agent-99" not in adapter.get_stats()

        asyncio.run(_run())

    def test_error_after_stats_cap_does_not_crash(self) -> None:
        """Error on over-cap agent must not crash (no-op on missing stats)."""
        async def _run() -> None:
            cfg = A2AClientConfig(stats_cap=1)
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx(), config=cfg)
            ok_client = _make_client()
            # Fill cap
            await adapter.send_message(
                agent_id="agent-0", message={}, tenant_id="t1",
                identity=_make_identity(), client=ok_client,
            )
            # Over-cap agent with error
            err_client = _make_client(raises=RuntimeError("fail"))
            result = await adapter.send_message(
                agent_id="agent-over-cap", message={}, tenant_id="t1",
                identity=_make_identity(), client=err_client,
            )
            assert result.success is False
            assert "agent-over-cap" not in adapter.get_stats()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Cost tracked in stats (Rule 7)
# ---------------------------------------------------------------------------


class TestCostTrackedInStatsDetailed:
    """Verify total_cost_usd side effect in stats."""

    def test_total_cost_usd_reflects_cost_per_message(self) -> None:
        async def _run() -> None:
            costs = {"agent-1": A2AMessageCost("agent-1", cost_per_message=0.05)}
            adapter = A2AClientContainmentAdapter(
                execution_context=_make_ctx(), message_costs=costs,
            )
            client = _make_client()
            await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            stats = adapter.get_stats()
            assert stats["agent-1"].total_cost_usd == pytest.approx(0.05, abs=0.001)

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Compound state: CB open + budget exhausted (Rule 6)
# ---------------------------------------------------------------------------


class TestCompoundState:
    def test_cb_open_plus_budget_exhausted(self) -> None:
        """CB check happens before budget -- CB error message wins."""
        async def _run() -> None:
            cb = CircuitBreaker(failure_threshold=1)
            cb.record_failure(error=RuntimeError("fail"))
            ctx = ExecutionContext(
                config=ExecutionConfig(max_cost_usd=0.0, max_steps=1000, max_retries_total=10)
            )
            adapter = A2AClientContainmentAdapter(
                execution_context=ctx, circuit_breaker=cb,
            )
            client = _make_client()
            result = await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            assert result.decision == Decision.HALT
            assert result.error == "Agent unavailable"  # CB wins, not budget
            client.send_message.assert_not_called()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Concurrent stats consistency (Rule 14)
# ---------------------------------------------------------------------------


class TestConcurrentStatsConsistency:
    def test_concurrent_calls_stats_count_correct(self) -> None:
        """10 concurrent calls must yield message_count == 10."""
        async def _run() -> None:
            adapter = A2AClientContainmentAdapter(
                execution_context=_make_ctx(max_cost=500.0),
            )
            client = _make_client()

            async def one_call(i: int) -> A2AResult:
                return await adapter.send_message(
                    agent_id="agent-1", message={"seq": i}, tenant_id="t1",
                    identity=_make_identity(), client=client,
                )

            results = await asyncio.gather(*[one_call(i) for i in range(10)])
            successful = [r for r in results if r.success]
            assert len(successful) == 10
            stats = adapter.get_stats()
            assert stats["agent-1"].message_count == 10

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# BoundA2AAdapter failure paths (Rule 15)
# ---------------------------------------------------------------------------


class TestBoundAdapterFailurePaths:
    def test_bound_adapter_cb_open(self) -> None:
        """BoundA2AAdapter must correctly propagate CB HALT."""
        async def _run() -> None:
            cb = CircuitBreaker(failure_threshold=1)
            cb.record_failure(error=RuntimeError("fail"))
            client = _make_client()
            adapter = wrap_a2a_agent(
                client=client, execution_context=_make_ctx(), circuit_breaker=cb,
            )
            result = await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(),
            )
            assert result.decision == Decision.HALT
            assert result.success is False

        asyncio.run(_run())

    def test_bound_adapter_budget_exhausted(self) -> None:
        """BoundA2AAdapter must correctly propagate budget HALT."""
        async def _run() -> None:
            ctx = ExecutionContext(
                config=ExecutionConfig(max_cost_usd=0.0, max_steps=1000, max_retries_total=10)
            )
            client = _make_client()
            adapter = wrap_a2a_agent(client=client, execution_context=ctx)
            result = await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(),
            )
            assert result.decision == Decision.HALT
            assert result.success is False

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Timeout error leakage (Rule 5)
# ---------------------------------------------------------------------------


class TestTimeoutErrorLeakage:
    def test_timeout_error_does_not_leak_type_name(self) -> None:
        async def _run() -> None:
            async def slow_send(_msg: Any) -> Any:
                await asyncio.sleep(10.0)
                return {}

            client = MagicMock()
            client.send_message = slow_send
            cfg = A2AClientConfig(timeout_seconds=0.05)
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx(), config=cfg)
            result = await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            assert result.success is False
            assert result.error is not None
            assert "TimeoutError" not in result.error
            assert "asyncio" not in result.error

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# get_stats_async (Rule 3)
# ---------------------------------------------------------------------------


class TestGetStatsAsync:
    def test_get_stats_async_returns_snapshot(self) -> None:
        async def _run() -> None:
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx())
            client = _make_client()
            await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            stats = await adapter.get_stats_async()
            assert "agent-1" in stats
            assert stats["agent-1"].message_count == 1
            # Verify snapshot isolation: mutating returned stats doesn't affect internal
            stats["agent-1"].message_count = 999
            stats2 = await adapter.get_stats_async()
            assert stats2["agent-1"].message_count == 1

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Whitespace agent_id / tenant_id (Round 3 fix)
# ---------------------------------------------------------------------------


class TestWhitespaceValidation:
    def test_whitespace_only_agent_id_raises(self) -> None:
        async def _run() -> None:
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx())
            client = _make_client()
            with pytest.raises(ValueError, match="agent_id"):
                await adapter.send_message(
                    agent_id="   ", message={}, tenant_id="t1",
                    identity=_make_identity(), client=client,
                )

        asyncio.run(_run())

    def test_whitespace_only_tenant_id_raises(self) -> None:
        async def _run() -> None:
            adapter = A2AClientContainmentAdapter(execution_context=_make_ctx())
            client = _make_client()
            with pytest.raises(ValueError, match="tenant_id"):
                await adapter.send_message(
                    agent_id="agent-1", message={}, tenant_id="  \t  ",
                    identity=_make_identity(), client=client,
                )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Error path cost tracked in stats (Round 3 fix, Rule 7)
# ---------------------------------------------------------------------------


class TestErrorPathCostTracking:
    def test_error_cost_added_to_stats(self) -> None:
        """Failed calls must still accumulate cost in stats.total_cost_usd."""
        async def _run() -> None:
            costs = {"agent-1": A2AMessageCost("agent-1", cost_per_message=0.05)}
            adapter = A2AClientContainmentAdapter(
                execution_context=_make_ctx(), message_costs=costs,
            )
            client = _make_client(raises=ConnectionError("down"))
            await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            stats = adapter.get_stats()
            assert stats["agent-1"].total_cost_usd == pytest.approx(0.05, abs=0.001)
            assert stats["agent-1"].error_count == 1

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# failure_predicate branch (Round 3 fix)
# ---------------------------------------------------------------------------


class TestFailurePredicate:
    def test_predicate_false_does_not_trip_cb(self) -> None:
        """failure_predicate returning False must prevent CB from recording failure."""
        async def _run() -> None:
            from veronica_core.runtime_policy import PolicyContext

            cb = CircuitBreaker(failure_threshold=1)
            adapter = A2AClientContainmentAdapter(
                execution_context=_make_ctx(),
                circuit_breaker=cb,
                failure_predicate=lambda _exc: False,
            )
            client = _make_client(raises=ConnectionError("net"))
            result = await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            assert result.success is False
            # CB must still be CLOSED -- predicate blocked the failure record
            cb_decision = cb.check(PolicyContext())
            assert cb_decision.allowed is True

        asyncio.run(_run())

    def test_predicate_true_trips_cb(self) -> None:
        """failure_predicate returning True must trip the CB."""
        async def _run() -> None:
            from veronica_core.runtime_policy import PolicyContext

            cb = CircuitBreaker(failure_threshold=1)
            adapter = A2AClientContainmentAdapter(
                execution_context=_make_ctx(),
                circuit_breaker=cb,
                failure_predicate=lambda _exc: True,
            )
            client = _make_client(raises=ConnectionError("net"))
            await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            cb_decision = cb.check(PolicyContext())
            assert cb_decision.allowed is False

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# avg_latency_ms correctness after CB-blocked calls (R7 fix)
# ---------------------------------------------------------------------------


class TestAvgLatencyMsCorrectness:
    def test_cb_blocked_calls_do_not_dilute_latency(self) -> None:
        """CB-blocked calls must not inflate the avg_latency_ms denominator."""
        async def _run() -> None:
            cb = CircuitBreaker(failure_threshold=1)
            adapter = A2AClientContainmentAdapter(
                execution_context=_make_ctx(max_cost=100.0),
                circuit_breaker=cb,
            )
            client = _make_client()
            # 1st call: success (records latency)
            r1 = await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            assert r1.success is True

            # Trip the circuit breaker
            cb.record_failure(error=RuntimeError("fail"))

            # 2nd call: CB-blocked (no latency)
            r2 = await adapter.send_message(
                agent_id="agent-1", message={}, tenant_id="t1",
                identity=_make_identity(), client=client,
            )
            assert r2.decision == Decision.HALT

            stats = adapter.get_stats()
            # avg_latency_ms denominator must be _latency_sample_count (1),
            # not message_count - error_count (2) which would halve the avg
            assert stats["agent-1"].message_count == 2  # total attempts
            assert stats["agent-1"]._latency_sample_count == 1  # only success
            # avg = _total_latency_ms / 1, not _total_latency_ms / 2
            assert stats["agent-1"].avg_latency_ms >= 0  # non-negative

        asyncio.run(_run())
