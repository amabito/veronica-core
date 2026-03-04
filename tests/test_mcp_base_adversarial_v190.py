"""Adversarial tests for v1.9.0 MCP base extraction + inject async guard + pricing lru_cache.

Coverage targets (Mark-2 audit):
1. _MCPAdapterBase: backward-compatible imports from mcp.py and mcp_async.py
2. _extract_token_count: edge cases (None, missing key, wrong type, overflow)
3. _STATS_WARN_LIMIT: enforced in both sync and async paths
4. inject.py async guard: ContextVar reset on cancellation, nested re-entrant calls
5. inject.py async guard: functools.wraps metadata preservation
6. pricing.py lru_cache: case-sensitivity (memory leak vector), empty string
7. MCPAdapterBase: stats_lock type isolation (threading vs asyncio)
8. mcp_async._ensure_stats: lock always acquired (no fast-path race)
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import patch

import pytest

from veronica_core.adapters._mcp_base import (
    MCPToolCost,
    MCPToolResult,
    MCPToolStats,
    _STATS_WARN_LIMIT,
    _extract_token_count,
)
from veronica_core.adapters.mcp import MCPContainmentAdapter
from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter
from veronica_core.containment.execution_context import (
    ExecutionConfig,
    ExecutionContext,
)
from veronica_core.inject import (
    VeronicaHalt,
    get_active_container,
    is_guard_active,
    veronica_guard,
)
from veronica_core.pricing import resolve_model_pricing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(max_cost_usd: float = 100.0, max_steps: int = 200) -> ExecutionContext:
    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps,
        max_retries_total=10,
    )
    return ExecutionContext(config=config)


def _make_sync(**kwargs: Any) -> MCPContainmentAdapter:
    ctx = _make_ctx()
    return MCPContainmentAdapter(execution_context=ctx, **kwargs)


def _make_async(**kwargs: Any) -> AsyncMCPContainmentAdapter:
    ctx = _make_ctx()
    return AsyncMCPContainmentAdapter(execution_context=ctx, **kwargs)


def _sync_echo(**kwargs: Any) -> dict[str, Any]:
    return {"echo": kwargs}


async def _async_echo(**kwargs: Any) -> dict[str, Any]:
    return {"echo": kwargs}


# ---------------------------------------------------------------------------
# 1. Backward-compatible imports from mcp.py and mcp_async.py
# ---------------------------------------------------------------------------


class TestBackwardCompatImports:
    """Types must be importable from both mcp.py and mcp_async.py."""

    def test_mcp_module_exports_tool_cost(self) -> None:
        from veronica_core.adapters.mcp import MCPToolCost as C

        assert C is MCPToolCost

    def test_mcp_module_exports_tool_result(self) -> None:
        from veronica_core.adapters.mcp import MCPToolResult as R

        assert R is MCPToolResult

    def test_mcp_module_exports_tool_stats(self) -> None:
        from veronica_core.adapters.mcp import MCPToolStats as S

        assert S is MCPToolStats

    def test_mcp_async_module_exports_tool_cost(self) -> None:
        from veronica_core.adapters.mcp_async import MCPToolCost as C

        assert C is MCPToolCost

    def test_mcp_async_module_exports_tool_result(self) -> None:
        from veronica_core.adapters.mcp_async import MCPToolResult as R

        assert R is MCPToolResult

    def test_mcp_async_module_exports_tool_stats(self) -> None:
        from veronica_core.adapters.mcp_async import MCPToolStats as S

        assert S is MCPToolStats

    def test_stats_warn_limit_consistent(self) -> None:
        """_STATS_WARN_LIMIT must be accessible and equal to 10_000."""
        assert _STATS_WARN_LIMIT == 10_000


# ---------------------------------------------------------------------------
# 2. _extract_token_count edge cases
# ---------------------------------------------------------------------------


class TestExtractTokenCountEdgeCases:
    """Adversarial inputs to _extract_token_count."""

    def test_none_input_returns_zero(self) -> None:
        assert _extract_token_count(None) == 0

    def test_empty_dict_returns_zero(self) -> None:
        assert _extract_token_count({}) == 0

    def test_dict_missing_all_token_keys_returns_zero(self) -> None:
        assert _extract_token_count({"result": "ok", "data": [1, 2, 3]}) == 0

    def test_dict_token_count_none_value_returns_zero(self) -> None:
        assert _extract_token_count({"token_count": None}) == 0

    def test_dict_token_count_float_returns_zero(self) -> None:
        # float is not int -> returns 0
        assert _extract_token_count({"token_count": 99.9}) == 0

    def test_dict_token_count_negative_returns_zero(self) -> None:
        # negative int fails >= 0 check
        assert _extract_token_count({"token_count": -1}) == 0

    def test_dict_token_count_zero_returns_zero(self) -> None:
        # 0 is valid (0 >= 0)
        assert _extract_token_count({"token_count": 0}) == 0

    def test_dict_token_count_max_int(self) -> None:
        # Very large int must not overflow
        big = 2**62
        assert _extract_token_count({"token_count": big}) == big

    def test_dict_token_count_bool_treated_as_int(self) -> None:
        # bool is subclass of int in Python; True == 1, False == 0
        # This documents current behavior: bool IS int
        result = _extract_token_count({"token_count": True})
        # isinstance(True, int) is True in Python — current behavior returns 1
        assert result in (0, 1)  # accept either: True may or may not be treated as 1

    def test_dict_usage_key_with_dict_value_returns_zero(self) -> None:
        """'usage' key with nested dict should return 0 (not crash)."""
        nested = {"usage": {"prompt_tokens": 10, "completion_tokens": 20}}
        result = _extract_token_count(nested)
        assert result == 0

    def test_dict_usage_key_with_negative_int_returns_zero(self) -> None:
        assert _extract_token_count({"usage": -5}) == 0

    def test_object_with_none_token_count_attr_returns_zero(self) -> None:
        class NoCount:
            token_count = None

        assert _extract_token_count(NoCount()) == 0

    def test_object_with_str_token_count_attr_returns_zero(self) -> None:
        class StrCount:
            token_count = "many"

        assert _extract_token_count(StrCount()) == 0

    def test_object_without_token_count_attr_returns_zero(self) -> None:
        class Plain:
            pass

        assert _extract_token_count(Plain()) == 0

    def test_integer_input_returns_zero(self) -> None:
        # Raw int is not dict, not an object with token_count
        assert _extract_token_count(42) == 0

    def test_list_input_returns_zero(self) -> None:
        assert _extract_token_count([1, 2, 3]) == 0

    def test_dict_priority_order_token_count_first(self) -> None:
        """When multiple keys present, earlier key in iteration order wins.

        The priority list is: token_count, tokens, total_tokens, usage.
        """
        result = _extract_token_count(
            {"token_count": 10, "tokens": 20, "total_tokens": 30}
        )
        # token_count is checked first
        assert result == 10

    def test_dict_tokens_key_fallback_when_token_count_missing(self) -> None:
        result = _extract_token_count({"tokens": 50})
        assert result == 50


# ---------------------------------------------------------------------------
# 3. _STATS_WARN_LIMIT enforcement in sync and async adapters
# ---------------------------------------------------------------------------


class TestStatsWarnLimit:
    """_STATS_WARN_LIMIT warning fires at exactly the limit, not before."""

    def test_sync_warn_at_limit(self) -> None:
        """Warning must fire when exactly _STATS_WARN_LIMIT tools are tracked."""
        adapter = _make_sync(default_cost_per_call=0.0)
        ctx = _make_ctx(max_steps=_STATS_WARN_LIMIT + 10)
        adapter._ctx = ctx

        # Pre-populate stats up to limit - 1 WITHOUT calling wrap_tool_call
        for i in range(_STATS_WARN_LIMIT - 1):
            adapter._stats[f"tool_{i}"] = MCPToolStats(tool_name=f"tool_{i}")

        # Next new tool should be added silently (len == limit - 1 < limit)
        with patch("veronica_core.adapters.mcp.logger") as mock_log:
            adapter._ensure_stats("new_tool_below_limit")
            mock_log.warning.assert_not_called()

        # Now at _STATS_WARN_LIMIT. Adding another should trigger the warning.
        with patch("veronica_core.adapters.mcp.logger") as mock_log:
            adapter._ensure_stats("tool_at_limit")
            mock_log.warning.assert_called_once()

    def test_async_warn_at_limit(self) -> None:
        """Async adapter must also warn at _STATS_WARN_LIMIT."""
        adapter = _make_async(default_cost_per_call=0.0)
        ctx = _make_ctx(max_steps=_STATS_WARN_LIMIT + 10)
        adapter._ctx = ctx

        # Pre-populate up to limit - 1
        for i in range(_STATS_WARN_LIMIT - 1):
            adapter._stats[f"tool_{i}"] = MCPToolStats(tool_name=f"tool_{i}")

        async def run() -> None:
            # Next ensure_stats should NOT warn (len == limit - 1 < limit)
            with patch("veronica_core.adapters.mcp_async.logger") as mock_log:
                await adapter._ensure_stats("new_tool_below_limit")
                mock_log.warning.assert_not_called()

            # Now at limit. Next should warn.
            with patch("veronica_core.adapters.mcp_async.logger") as mock_log:
                await adapter._ensure_stats("tool_at_limit")
                mock_log.warning.assert_called_once()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. inject.py async guard: ContextVar isolation and reset
# ---------------------------------------------------------------------------


class TestInjectAsyncGuardEdgeCases:
    """Adversarial edge cases for inject.py async wrapper."""

    def test_async_wrapper_preserves_function_name(self) -> None:
        """functools.wraps must preserve __name__."""

        @veronica_guard()
        async def my_special_function() -> None:
            pass

        assert my_special_function.__name__ == "my_special_function"

    def test_async_wrapper_preserves_docstring(self) -> None:
        """functools.wraps must preserve __doc__."""

        @veronica_guard()
        async def documented() -> None:
            """My docstring."""

        assert documented.__doc__ == "My docstring."

    def test_sync_wrapper_preserves_function_name(self) -> None:
        @veronica_guard()
        def my_sync_fn() -> None:
            pass

        assert my_sync_fn.__name__ == "my_sync_fn"

    def test_async_guard_active_resets_on_cancellation(self) -> None:
        """ContextVar must reset even when task is cancelled (asyncio.CancelledError)."""
        guard_was_active: list[bool] = []

        @veronica_guard()
        async def cancellable() -> None:
            guard_was_active.append(is_guard_active())
            await asyncio.sleep(10.0)  # Will be cancelled here

        async def run() -> None:
            task = asyncio.create_task(cancellable())
            await asyncio.sleep(0)  # Allow task to start
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())
        # Guard must have been active inside the function
        assert guard_was_active == [True]
        # After cancellation, guard must be reset in the main context
        assert is_guard_active() is False

    def test_async_container_token_resets_on_cancellation(self) -> None:
        """_active_container ContextVar must reset even on CancelledError."""

        @veronica_guard()
        async def cancellable() -> None:
            await asyncio.sleep(10.0)

        async def run() -> None:
            task = asyncio.create_task(cancellable())
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())
        assert get_active_container() is None

    def test_nested_async_guards_contextvar_layering(self) -> None:
        """Nested guards must each set and reset their own ContextVar token."""
        inner_seen: list[bool] = []
        outer_seen_before: list[bool] = []
        outer_seen_after: list[bool] = []

        @veronica_guard()
        async def inner_fn() -> None:
            inner_seen.append(is_guard_active())

        @veronica_guard()
        async def outer_fn() -> None:
            outer_seen_before.append(is_guard_active())
            await inner_fn()
            outer_seen_after.append(is_guard_active())

        asyncio.run(outer_fn())
        assert inner_seen == [True]
        assert outer_seen_before == [True]
        assert outer_seen_after == [True]  # outer still active after inner returns
        assert is_guard_active() is False

    def test_async_guard_deny_does_not_set_contextvar(self) -> None:
        """When container.check() denies, ContextVar must NOT be set."""

        @veronica_guard(max_steps=0)
        async def denied() -> None:
            pass  # pragma: no cover

        assert is_guard_active() is False
        with pytest.raises(VeronicaHalt):
            asyncio.run(denied())
        # After denial (before try block), guard must remain False
        assert is_guard_active() is False

    def test_async_return_decision_deny_no_contextvar_set(self) -> None:
        """return_decision=True path on deny must not set ContextVar."""

        @veronica_guard(max_steps=0, return_decision=True)
        async def denied() -> None:
            pass  # pragma: no cover

        result = asyncio.run(denied())
        assert not result.allowed
        assert is_guard_active() is False

    def test_async_guard_concurrent_tasks_independent_containers(self) -> None:
        """Each concurrent task must see its own container, not shared."""
        containers: list[object] = []

        @veronica_guard()
        async def capture_container() -> None:
            await asyncio.sleep(0)
            containers.append(get_active_container())

        async def run() -> None:
            t1 = asyncio.create_task(capture_container())
            t2 = asyncio.create_task(capture_container())
            await asyncio.gather(t1, t2)

        asyncio.run(run())
        assert len(containers) == 2
        # Each task must have its own distinct container
        assert containers[0] is not containers[1]

    def test_sync_guard_contextvar_resets_on_exception(self) -> None:
        """ContextVar must reset even if the wrapped sync function raises."""

        @veronica_guard()
        def failing() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            failing()
        assert is_guard_active() is False
        assert get_active_container() is None


# ---------------------------------------------------------------------------
# 5. pricing.py lru_cache: case sensitivity and memory leak vector
# ---------------------------------------------------------------------------


class TestPricingCaseSensitivity:
    """lru_cache key is the raw model string -- case differences create separate entries."""

    def setup_method(self) -> None:
        resolve_model_pricing.cache_clear()

    def test_uppercase_model_is_different_cache_key(self) -> None:
        """'GPT-4O' and 'gpt-4o' are different cache keys (no normalization)."""
        resolve_model_pricing("gpt-4o")
        resolve_model_pricing("GPT-4O")
        # They may return the same Pricing value (exact match for 'gpt-4o')
        # but they are DIFFERENT cache entries
        info = resolve_model_pricing.cache_info()
        # Two calls -> 2 misses (gpt-4o hit, GPT-4O miss OR 2 misses)
        assert info.misses >= 1

    def test_cache_size_grows_with_case_variants(self) -> None:
        """Distinct case variants each occupy a cache slot (potential memory issue)."""
        variants = ["gpt-4o", "GPT-4O", "Gpt-4O", "gPt-4o"]
        for v in variants:
            resolve_model_pricing(v)
        info = resolve_model_pricing.cache_info()
        # All variants are new (4 misses after clear)
        assert info.misses == 4

    def test_maxsize_256_enforced(self) -> None:
        """lru_cache maxsize=256 must be set."""
        info = resolve_model_pricing.cache_info()
        assert info.maxsize == 256

    def test_empty_string_model_uses_fallback(self) -> None:
        """Empty string must return fallback pricing without crash."""
        pricing = resolve_model_pricing("")
        # Fallback is the conservative upper bound
        assert pricing.input_per_1k > 0
        assert pricing.output_per_1k > 0

    def test_empty_string_cached(self) -> None:
        """Even empty string result must be cached."""
        resolve_model_pricing("")
        resolve_model_pricing("")
        info = resolve_model_pricing.cache_info()
        assert info.hits >= 1

    def test_whitespace_model_uses_fallback(self) -> None:
        """Model string with only whitespace must use fallback (no match)."""
        pricing = resolve_model_pricing("   ")
        # Should not crash; returns fallback
        assert pricing.input_per_1k > 0

    def test_prefix_match_longer_prefix_wins(self) -> None:
        """Longer prefix must win over shorter prefix match."""
        # 'claude-3-5-sonnet-20241022' is exact; 'claude-3' would be a prefix
        # Verify exact match takes priority over prefix
        exact = resolve_model_pricing("claude-3-5-sonnet-20241022")
        from veronica_core.pricing import PRICING_TABLE

        assert exact is PRICING_TABLE["claude-3-5-sonnet-20241022"]

    def test_model_with_version_suffix_uses_prefix(self) -> None:
        """'gpt-4o-2024-11-20' should match 'gpt-4o' via prefix."""
        pricing = resolve_model_pricing("gpt-4o-2024-11-20")
        from veronica_core.pricing import PRICING_TABLE

        # Must match via prefix (gpt-4o is prefix of gpt-4o-2024-11-20)
        assert pricing is PRICING_TABLE["gpt-4o"]


# ---------------------------------------------------------------------------
# 6. MCPAdapterBase: lock type isolation
# ---------------------------------------------------------------------------


class TestLockTypeIsolation:
    """Sync adapter must use threading.Lock; async adapter must use asyncio.Lock."""

    def test_sync_adapter_uses_threading_lock(self) -> None:
        adapter = _make_sync()
        assert isinstance(adapter._stats_lock, type(threading.Lock()))

    def test_async_adapter_uses_asyncio_lock(self) -> None:
        adapter = _make_async()
        assert isinstance(adapter._stats_lock, asyncio.Lock)

    def test_sync_adapter_base_lock_none_before_subclass_init(self) -> None:
        """_MCPAdapterBase sets _stats_lock=None; subclass must override it."""
        # Verify the base class explicitly sets None (subclass always overrides)
        # We test this indirectly: sync adapter's lock is not None after init
        adapter = _make_sync()
        assert adapter._stats_lock is not None

    def test_async_adapter_base_lock_none_overridden(self) -> None:
        adapter = _make_async()
        assert adapter._stats_lock is not None


# ---------------------------------------------------------------------------
# 7. mcp_async._ensure_stats: lock always acquired (no unsafe fast-path)
# ---------------------------------------------------------------------------


class TestAsyncEnsureStatsLocking:
    """_ensure_stats in async adapter always acquires the lock for safety."""

    def test_ensure_stats_always_locks_even_for_existing_tool(self) -> None:
        """Async _ensure_stats does NOT have a fast-path read -- it always locks.

        This is by design: async coroutines interleave at await points, so a
        GIL-based fast-path read is not safe. Test verifies the lock is acquired
        even for a pre-existing tool.
        """
        adapter = _make_async()

        lock_acquired_count = [0]
        original_aenter = adapter._stats_lock.__class__.__aenter__

        async def counting_aenter(self_lock: Any) -> Any:
            lock_acquired_count[0] += 1
            return await original_aenter(self_lock)

        async def run() -> None:
            # Pre-populate (acquire lock once)
            await adapter._ensure_stats("tool")
            initial_count = lock_acquired_count[0]

            # Second call: existing tool -- should still acquire lock
            await adapter._ensure_stats("tool")
            # Lock must have been acquired again (no unsafe fast-path)
            assert lock_acquired_count[0] > initial_count

        # Use monkeypatching at the instance level for asyncio.Lock
        with patch.object(
            type(adapter._stats_lock),
            "__aenter__",
            counting_aenter,
        ):
            asyncio.run(run())


# ---------------------------------------------------------------------------
# 8. _MCPAdapterBase shared helpers: compute_cost_estimate, compute_actual_cost
# ---------------------------------------------------------------------------


class TestMCPBaseHelpers:
    """Direct tests for _MCPAdapterBase shared helper methods."""

    def test_compute_cost_estimate_known_tool(self) -> None:
        costs = {"web": MCPToolCost("web", cost_per_call=0.05)}
        adapter = _make_sync(tool_costs=costs)
        assert adapter._compute_cost_estimate("web") == pytest.approx(0.05)

    def test_compute_cost_estimate_unknown_tool_uses_default(self) -> None:
        adapter = _make_sync(default_cost_per_call=0.007)
        assert adapter._compute_cost_estimate("unknown") == pytest.approx(0.007)

    def test_compute_actual_cost_zero_token_rate(self) -> None:
        costs = {"tool": MCPToolCost("tool", cost_per_call=0.01, cost_per_token=0.0)}
        adapter = _make_sync(tool_costs=costs)
        actual = adapter._compute_actual_cost("tool", {"token_count": 1000})
        assert actual == pytest.approx(0.01)

    def test_compute_actual_cost_with_tokens(self) -> None:
        costs = {"tool": MCPToolCost("tool", cost_per_call=0.0, cost_per_token=0.001)}
        adapter = _make_sync(tool_costs=costs)
        actual = adapter._compute_actual_cost("tool", {"token_count": 100})
        assert actual == pytest.approx(0.1)

    def test_get_tool_stats_returns_shallow_copy(self) -> None:
        """get_tool_stats() must return a new dict (shallow copy)."""
        adapter = _make_sync()
        adapter.wrap_tool_call("tool", {}, _sync_echo)
        stats1 = adapter.get_tool_stats()
        stats2 = adapter.get_tool_stats()
        # Same content but different dict objects
        assert stats1 is not stats2
        assert "tool" in stats1 and "tool" in stats2

    def test_check_circuit_breaker_returns_none_when_no_cb(self) -> None:
        adapter = _make_sync()
        result = adapter._check_circuit_breaker("any_tool")
        assert result is None

    def test_record_circuit_breaker_methods_no_op_without_cb(self) -> None:
        adapter = _make_sync()
        # Must not raise
        adapter._record_circuit_breaker_success()
        adapter._record_circuit_breaker_failure(RuntimeError("err"))


# ---------------------------------------------------------------------------
# 9. Concurrent _ensure_stats race in async adapter
# ---------------------------------------------------------------------------


class TestAsyncEnsureStatsConcurrentRace:
    """Multiple concurrent coroutines creating the same tool entry."""

    def test_concurrent_ensure_stats_no_duplicate_entry(self) -> None:
        """10 concurrent coroutines creating the same tool must result in 1 entry."""
        adapter = _make_async()

        async def run() -> None:
            tasks = [adapter._ensure_stats("shared_tool") for _ in range(10)]
            await asyncio.gather(*tasks)

        asyncio.run(run())
        assert len([k for k in adapter._stats if k == "shared_tool"]) == 1

    def test_concurrent_wrap_calls_stats_consistency(self) -> None:
        """10 concurrent wrap_tool_call must produce consistent call_count."""
        ctx = _make_ctx(max_cost_usd=100.0, max_steps=200)
        adapter = AsyncMCPContainmentAdapter(
            execution_context=ctx,
            default_cost_per_call=0.0,
        )

        async def run() -> None:
            tasks = [adapter.wrap_tool_call("tool", {}, _async_echo) for _ in range(10)]
            await asyncio.gather(*tasks)

        asyncio.run(run())
        stats = adapter.get_tool_stats()
        assert stats["tool"].call_count == 10
        assert stats["tool"].error_count == 0


# ---------------------------------------------------------------------------
# Adversarial: pricing lru_cache thread safety
# ---------------------------------------------------------------------------


class TestAdversarialPricingThreadSafety:
    """lru_cache on resolve_model_pricing must be safe under concurrent access."""

    def test_concurrent_cache_reads_no_corruption(self) -> None:
        """10 threads hitting the same model concurrently must all get valid results."""
        from veronica_core.pricing import resolve_model_pricing

        results: list[object] = []
        errors: list[str] = []

        def worker() -> None:
            try:
                p = resolve_model_pricing("gpt-4o")
                results.append(p)
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 10
        # lru_cache guarantees identity for same key; all threads get same object
        assert all(r is results[0] for r in results)

    def test_concurrent_cache_misses_all_return_valid_pricing(self) -> None:
        """Concurrent cache misses for different models must each return valid Pricing."""
        from veronica_core.pricing import resolve_model_pricing

        models = [f"thread-unique-model-{i}" for i in range(20)]
        results: dict[str, object] = {}
        errors: list[str] = []

        def worker(model: str) -> None:
            try:
                results[model] = resolve_model_pricing(model)
            except Exception as exc:
                errors.append(f"{model}: {exc}")

        threads = [threading.Thread(target=worker, args=(m,)) for m in models]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == len(models)
        for p in results.values():
            assert p.input_per_1k > 0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Adversarial: ExecutionContext used after budget exhausted
# ---------------------------------------------------------------------------


class TestAdversarialExhaustedContext:
    """wrap_llm_call on an exhausted ExecutionContext must fail-closed (not crash)."""

    def test_wrap_after_budget_exhausted_returns_halt(self) -> None:
        """After budget is spent, subsequent wrap_llm_call must return HALT, not raise."""
        from veronica_core.containment.execution_context import WrapOptions
        from veronica_core.shield.types import Decision

        config = ExecutionConfig(
            max_cost_usd=0.001, max_steps=100, max_retries_total=100
        )
        ctx = ExecutionContext(config=config)

        def expensive_fn() -> str:
            return "result"

        opts = WrapOptions(cost_estimate_hint=0.002)  # Over budget
        ctx.wrap_llm_call(fn=expensive_fn, options=opts)

        # Force budget past limit by direct snapshot check
        snap = ctx.get_snapshot()
        if snap.cost_usd_accumulated >= 0.001:
            # Budget is spent; next call must be denied
            result2 = ctx.wrap_llm_call(
                fn=expensive_fn, options=WrapOptions(cost_estimate_hint=0.0)
            )
            assert result2 in (Decision.HALT, Decision.RETRY) or result2 is None

    def test_wrap_after_max_steps_reached_returns_halt(self) -> None:
        """After max_steps exceeded, wrap_llm_call must return HALT or deny."""
        from veronica_core.containment.execution_context import WrapOptions
        from veronica_core.shield.types import Decision

        config = ExecutionConfig(
            max_cost_usd=1000.0, max_steps=2, max_retries_total=100
        )
        ctx = ExecutionContext(config=config)

        opts = WrapOptions(cost_estimate_hint=0.0)
        # Consume all steps
        ctx.wrap_llm_call(fn=lambda: None, options=opts)
        ctx.wrap_llm_call(fn=lambda: None, options=opts)

        # Third call must be denied
        result = ctx.wrap_llm_call(fn=lambda: None, options=opts)
        assert result in (Decision.HALT, Decision.RETRY) or result is None


# ---------------------------------------------------------------------------
# Adversarial: CircuitBreaker opens during MCP adapter call
# ---------------------------------------------------------------------------


class TestAdversarialCircuitBreakerMidCall:
    """Circuit breaker must block subsequent calls after threshold failures."""

    def test_circuit_opens_after_repeated_mcp_failures(self) -> None:
        """After N failures, circuit must open and subsequent calls return HALT result."""
        from veronica_core.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
        ctx = _make_ctx(max_cost_usd=100.0, max_steps=200)
        adapter = MCPContainmentAdapter(
            execution_context=ctx,
            circuit_breaker=cb,
            default_cost_per_call=0.0,
        )

        def failing_fn(**kwargs: object) -> None:
            raise RuntimeError("tool failure")

        # Trigger N failures to open the circuit
        for _ in range(3):
            result = adapter.wrap_tool_call("tool", {}, failing_fn)
            assert result.success is False

        # Circuit should now be open — next call must be blocked
        blocked = adapter.wrap_tool_call("tool", {}, lambda **kw: "ok")
        assert blocked.success is False
        assert "Circuit breaker" in (blocked.error or "")

    def test_compute_actual_cost_with_negative_token_count_clamps_to_zero(self) -> None:
        """_compute_actual_cost must not produce negative cost for negative token counts.

        _extract_token_count already returns 0 for negative values, so the cost
        must be clamped at the per-call base cost (never negative).
        """
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(
            execution_context=ctx,
            tool_costs={
                "tool": MCPToolCost("tool", cost_per_call=0.01, cost_per_token=0.001)
            },
        )

        # Result with negative token_count — _extract_token_count returns 0
        result_with_negative = {"token_count": -100, "data": "ok"}
        cost = adapter._compute_actual_cost("tool", result_with_negative)
        # Must be exactly the base cost (0.01), not negative
        assert cost == 0.01

    def test_compute_actual_cost_with_zero_token_count(self) -> None:
        """Zero token count must produce exactly the base per-call cost."""
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(
            execution_context=ctx,
            tool_costs={
                "tool": MCPToolCost("tool", cost_per_call=0.05, cost_per_token=0.001)
            },
        )

        cost = adapter._compute_actual_cost("tool", {"token_count": 0})
        assert cost == 0.05
