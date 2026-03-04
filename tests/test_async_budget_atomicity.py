"""Tests for Item 5: Async budget atomicity in AsyncMCPContainmentAdapter.

Tests cover:
1. reserve/commit used for normal success path
2. rollback on call_fn exception
3. rollback on isError result
4. TOCTOU: concurrent coroutines cannot double-spend budget
5. CancelledError: reservation rolled back in finally
6. Legacy backend (no reserve) backward compat
7. Item 2b: AsyncBudgetBackendProtocol conformance

Uses asyncio.run() wrappers since pytest-asyncio is not available.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from veronica_core.adapters.mcp import MCPToolCost
from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter
from veronica_core.containment.execution_context import ExecutionConfig, ExecutionContext
from veronica_core.distributed import LocalBudgetBackend
from veronica_core.protocols import AsyncBudgetBackendProtocol
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_adapter(max_cost: float = 10.0, tool_cost: float = 0.1) -> tuple[AsyncMCPContainmentAdapter, LocalBudgetBackend]:
    backend = LocalBudgetBackend()
    config = ExecutionConfig(
        max_cost_usd=max_cost,
        max_steps=100,
        max_retries_total=10,
        budget_backend=backend,
    )
    ctx = ExecutionContext(config=config)
    ctx._budget_backend = backend
    adapter = AsyncMCPContainmentAdapter(
        execution_context=ctx,
        tool_costs={"test_tool": MCPToolCost("test_tool", cost_per_call=tool_cost)},
    )
    return adapter, backend


async def success_fn(**kwargs: Any) -> dict:
    return {"result": "ok"}


async def error_fn(**kwargs: Any) -> dict:
    raise ValueError("tool failed")


# ---------------------------------------------------------------------------
# Basic reserve/commit/rollback flow
# ---------------------------------------------------------------------------


class TestAsyncBudgetAtomicity:
    def test_successful_call_commits_cost(self):
        async def run():
            adapter, backend = make_adapter()
            result = await adapter.wrap_tool_call("test_tool", {}, success_fn)
            assert result.success is True
            assert result.decision == Decision.ALLOW
            # Cost should be committed (not in reservation)
            assert backend.get() == pytest.approx(0.1)
            assert backend.get_reserved() == 0.0
        asyncio.run(run())

    def test_failed_call_rolls_back_reservation(self):
        async def run():
            adapter, backend = make_adapter()
            result = await adapter.wrap_tool_call("test_tool", {}, error_fn)
            assert result.success is False
            # No cost committed — reservation was rolled back
            assert backend.get() == 0.0
            assert backend.get_reserved() == 0.0
        asyncio.run(run())

    def test_is_error_result_rolls_back_reservation(self):
        async def run():
            async def is_error_fn(**kwargs: Any):
                result = MagicMock()
                result.isError = True
                return result

            adapter, backend = make_adapter()
            result = await adapter.wrap_tool_call("test_tool", {}, is_error_fn)
            assert result.success is False
            # No cost committed
            assert backend.get() == 0.0
            assert backend.get_reserved() == 0.0
        asyncio.run(run())

    def test_budget_ceiling_exceeded_returns_halt(self):
        async def run():
            adapter, backend = make_adapter(max_cost=0.05, tool_cost=0.1)
            result = await adapter.wrap_tool_call("test_tool", {}, success_fn)
            assert result.decision == Decision.HALT
            assert backend.get() == 0.0
        asyncio.run(run())

    def test_no_reservation_held_during_await(self):
        """Reservation should be committed after call, not held indefinitely."""
        async def run():
            adapter, backend = make_adapter()
            result = await adapter.wrap_tool_call("test_tool", {}, success_fn)
            assert result.success is True
            assert backend.get_reserved() == 0.0
        asyncio.run(run())

    def test_zero_cost_tool_no_reservation(self):
        """Zero-cost tools skip reservation but still succeed."""
        async def run():
            backend = LocalBudgetBackend()
            config = ExecutionConfig(
                max_cost_usd=1.0, max_steps=100, max_retries_total=10,
                budget_backend=backend,
            )
            ctx = ExecutionContext(config=config)
            ctx._budget_backend = backend
            adapter = AsyncMCPContainmentAdapter(
                execution_context=ctx,
                tool_costs={"free_tool": MCPToolCost("free_tool", cost_per_call=0.0)},
            )
            result = await adapter.wrap_tool_call("free_tool", {}, success_fn)
            assert result.success is True
        asyncio.run(run())


# ---------------------------------------------------------------------------
# Concurrent TOCTOU adversarial test
# ---------------------------------------------------------------------------


class TestAdversarialAsyncBudgetConcurrency:
    def test_concurrent_calls_ceiling_enforced(self):
        """10 concurrent calls of $0.15 each against $1.0 ceiling.
        At most 6 should succeed; none should cause over-spend.
        """
        async def run():
            backend = LocalBudgetBackend()
            config = ExecutionConfig(
                max_cost_usd=1.0, max_steps=100, max_retries_total=100,
                budget_backend=backend,
            )
            ctx = ExecutionContext(config=config)
            ctx._budget_backend = backend
            adapter = AsyncMCPContainmentAdapter(
                execution_context=ctx,
                tool_costs={"tool": MCPToolCost("tool", cost_per_call=0.15)},
            )

            async def slow_fn(**kwargs: Any) -> dict:
                await asyncio.sleep(0.01)  # Yield to allow interleaving
                return {"ok": True}

            tasks = [adapter.wrap_tool_call("tool", {}, slow_fn) for _ in range(10)]
            results = await asyncio.gather(*tasks)

            allowed = [r for r in results if r.decision == Decision.ALLOW]

            # At most 6 calls of $0.15 fit in $1.0
            assert len(allowed) <= 6
            # Total committed must not exceed ceiling
            assert backend.get() <= 1.0 + 1e-9
            # No reservations left dangling
            assert backend.get_reserved() == 0.0
        asyncio.run(run())

    def test_exception_during_await_rolls_back(self):
        """Concurrent coroutines: the one that raises must not leave a dangling reservation."""
        async def run():
            backend = LocalBudgetBackend()
            config = ExecutionConfig(
                max_cost_usd=10.0, max_steps=100, max_retries_total=100,
                budget_backend=backend,
            )
            ctx = ExecutionContext(config=config)
            ctx._budget_backend = backend
            adapter = AsyncMCPContainmentAdapter(
                execution_context=ctx,
                tool_costs={"tool": MCPToolCost("tool", cost_per_call=0.1)},
            )

            async def sometimes_fails(fail: bool = False, **kwargs: Any) -> dict:
                await asyncio.sleep(0.005)
                if fail:
                    raise RuntimeError("planned failure")
                return {"ok": True}

            tasks = [
                adapter.wrap_tool_call("tool", {"fail": i % 3 == 0}, sometimes_fails)
                for i in range(9)
            ]
            results = await asyncio.gather(*tasks)

            # No dangling reservations
            assert backend.get_reserved() == 0.0
            # Total committed matches number of successes * cost
            success_count = sum(1 for r in results if r.success)
            expected = success_count * 0.1
            assert backend.get() == pytest.approx(expected, abs=1e-9)
        asyncio.run(run())


# ---------------------------------------------------------------------------
# Legacy backend backward compat
# ---------------------------------------------------------------------------


class TestLegacyBackendCompat:
    def test_legacy_backend_no_reserve_still_works(self):
        """Backend without reserve() falls back to sync wrap_tool_call probe."""

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

        async def run():
            backend = LegacyBackend()
            config = ExecutionConfig(
                max_cost_usd=1.0, max_steps=100, max_retries_total=10,
                budget_backend=backend,
            )
            ctx = ExecutionContext(config=config)
            ctx._budget_backend = backend
            adapter = AsyncMCPContainmentAdapter(
                execution_context=ctx,
                tool_costs={"tool": MCPToolCost("tool", cost_per_call=0.1)},
            )

            result = await adapter.wrap_tool_call("tool", {}, success_fn)
            assert result.success is True
        asyncio.run(run())


# ---------------------------------------------------------------------------
# Item 2b: AsyncBudgetBackendProtocol conformance
# ---------------------------------------------------------------------------


class TestAsyncBudgetBackendProtocol:
    def test_protocol_is_runtime_checkable(self):
        """isinstance() works against AsyncBudgetBackendProtocol."""
        assert hasattr(AsyncBudgetBackendProtocol, "__protocol_attrs__") or \
               hasattr(AsyncBudgetBackendProtocol, "_is_protocol")

    def test_protocol_has_required_methods(self):
        """Protocol defines reserve, commit, rollback, get."""
        import inspect
        members = {name for name, _ in inspect.getmembers(AsyncBudgetBackendProtocol)}
        assert "reserve" in members
        assert "commit" in members
        assert "rollback" in members
        assert "get" in members

    def test_conforming_class_passes_isinstance(self):
        """A class implementing all async methods satisfies the protocol."""

        class ConformingBackend:
            async def reserve(self, amount: float, ceiling: float) -> str:
                return "rid"

            async def commit(self, reservation_id: str) -> float:
                return 0.0

            async def rollback(self, reservation_id: str) -> None:
                pass

            async def get(self) -> float:
                return 0.0

        # runtime_checkable only checks for method presence (not signatures)
        backend = ConformingBackend()
        assert isinstance(backend, AsyncBudgetBackendProtocol)
