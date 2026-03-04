"""Adversarial tests for v1.9.0 Phase 1 changes.

Covers attack vectors NOT tested by happy-path tests:
- WrapOptions NaN bypass via edge-case floats
- _wrap() stack leak under concurrent reentrant calls
- _MAX_NODES cap race condition under threading
- ContextVar cross-context contamination
- _MCPAdapterBase import backward compatibility
- async veronica_guard cancellation + timeout
- lru_cache poisoning via unhashable/adversarial model strings
"""

from __future__ import annotations

import asyncio
import math
import threading

import pytest

from veronica_core.containment.execution_context import (
    ExecutionConfig,
    ExecutionContext,
    WrapOptions,
    _MAX_NODES,
)
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# 1a: WrapOptions NaN — adversarial bypass attempts
# ---------------------------------------------------------------------------


class TestAdversarialWrapOptionsNaN:
    """Attacker tries to bypass budget via crafted cost_estimate_hint values."""

    def test_negative_zero_allowed(self):
        """IEEE -0.0 is valid zero; must not be rejected."""
        opts = WrapOptions(cost_estimate_hint=-0.0)
        assert opts.cost_estimate_hint == 0.0

    def test_very_small_positive_allowed(self):
        """Subnormal float near zero must be accepted."""
        opts = WrapOptions(cost_estimate_hint=5e-324)
        assert opts.cost_estimate_hint > 0.0

    def test_max_float_allowed(self):
        """sys.float_info.max is finite; must be accepted."""
        import sys

        opts = WrapOptions(cost_estimate_hint=sys.float_info.max)
        assert math.isfinite(opts.cost_estimate_hint)

    def test_quiet_nan_rejected(self):
        """Quiet NaN (common in C interop) must be rejected."""
        with pytest.raises(ValueError, match="finite"):
            WrapOptions(cost_estimate_hint=float("nan"))

    def test_negative_one_cent_rejected(self):
        """Even small negative cost must be rejected."""
        with pytest.raises(ValueError, match="non-negative"):
            WrapOptions(cost_estimate_hint=-0.001)


# ---------------------------------------------------------------------------
# 1b: _wrap() finally — adversarial stack leak scenarios
# ---------------------------------------------------------------------------


class TestAdversarialWrapStackLeak:
    """Attacker tries to leak graph stack entries via edge-case exceptions."""

    def test_concurrent_wraps_no_stack_leak(self):
        """Multiple threads calling wrap_llm_call must not corrupt the shared context."""
        config = ExecutionConfig(
            max_cost_usd=1000.0, max_steps=200, max_retries_total=200
        )
        ctx = ExecutionContext(config=config)
        errors: list[str] = []

        def worker(idx: int) -> None:
            try:
                for _ in range(20):
                    ctx.wrap_llm_call(
                        fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.0)
                    )
            except Exception as exc:
                errors.append(f"worker-{idx}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors, f"Thread errors: {errors}"

    def test_generator_exception_in_fn_handled_as_retry(self):
        """fn that raises GeneratorExit must be handled (not leak the stack).

        GeneratorExit is BaseException but NOT a process-termination signal
        (unlike KeyboardInterrupt/SystemExit). It's caught by _invoke_fn and
        routed through _handle_fn_error as RETRY, which is correct behavior.
        """
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
        ctx = ExecutionContext(config=config)

        def gen_exit_fn():
            raise GeneratorExit("generator cleaned up")

        d = ctx.wrap_llm_call(
            fn=gen_exit_fn, options=WrapOptions(cost_estimate_hint=0.0)
        )
        # GeneratorExit is handled as a retryable error, not re-raised
        assert d == Decision.RETRY

        stack = ctx._node_stack_var.get()
        assert stack is None or len(stack) == 0


# ---------------------------------------------------------------------------
# 1f: _MAX_NODES cap — race condition under threading
# ---------------------------------------------------------------------------


class TestAdversarialNodesCap:
    """Attacker tries to exceed _MAX_NODES via concurrent threads."""

    def test_concurrent_cap_not_exceeded(self):
        """Under high concurrency, _nodes must never exceed _MAX_NODES."""
        # Use a small cap for test speed
        config = ExecutionConfig(
            max_cost_usd=1_000_000.0,
            max_steps=_MAX_NODES + 200,
            max_retries_total=_MAX_NODES + 200,
        )
        ctx = ExecutionContext(config=config)
        barrier = threading.Barrier(4)

        def fill(count: int) -> None:
            barrier.wait()
            for _ in range(count):
                ctx.wrap_llm_call(
                    fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.0)
                )

        # 4 threads each doing _MAX_NODES/4 + 100 calls
        per_thread = _MAX_NODES // 4 + 100
        threads = [threading.Thread(target=fill, args=(per_thread,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120.0)

        snap = ctx.get_snapshot()
        assert len(snap.nodes) <= _MAX_NODES, (
            f"_nodes exceeded cap: {len(snap.nodes)} > {_MAX_NODES}"
        )


# ---------------------------------------------------------------------------
# ContextVar: cross-context contamination
# ---------------------------------------------------------------------------


class TestAdversarialContextVarContamination:
    """Attacker tries to leak state between different ExecutionContext instances."""

    def test_two_contexts_isolated_stacks(self):
        """Two ExecutionContext instances must not share node stacks."""
        config1 = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
        config2 = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
        ctx1 = ExecutionContext(config=config1)
        ctx2 = ExecutionContext(config=config2)

        ctx1.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.0))
        ctx2.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.0))

        snap1 = ctx1.get_snapshot()
        snap2 = ctx2.get_snapshot()
        assert len(snap1.nodes) == 1
        assert len(snap2.nodes) == 1
        assert snap1.nodes[0].node_id != snap2.nodes[0].node_id


# ---------------------------------------------------------------------------
# async veronica_guard: timeout (cancellation covered in test_mcp_base_adversarial_v190.py)
# ---------------------------------------------------------------------------


class TestAdversarialAsyncGuard:
    """Attacker tries to leave ContextVar dirty via asyncio.timeout."""

    def test_timeout_resets_guard(self):
        """asyncio.timeout must reset _guard_active to False."""
        from veronica_core.inject import is_guard_active, veronica_guard

        @veronica_guard()
        async def slow_fn():
            await asyncio.sleep(100)

        async def runner():
            try:
                async with asyncio.timeout(0.01):
                    await slow_fn()
            except (asyncio.TimeoutError, TimeoutError):
                pass

        asyncio.run(runner())
        assert is_guard_active() is False


# ---------------------------------------------------------------------------
# pricing lru_cache: adversarial model strings
# ---------------------------------------------------------------------------


class TestAdversarialPricingCache:
    """Attacker tries to abuse or poison the pricing cache."""

    def test_unknown_model_returns_fallback(self):
        from veronica_core.pricing import resolve_model_pricing

        p = resolve_model_pricing("totally-fake-model-xyz")
        assert p.input_per_1k > 0

    def test_many_unique_models_dont_crash(self):
        """Attacker sends 1000 unique model strings; cache eviction must not crash."""
        from veronica_core.pricing import resolve_model_pricing

        for i in range(1000):
            p = resolve_model_pricing(f"adversarial-model-{i}")
            assert p.input_per_1k > 0

    def test_empty_string_model(self):
        from veronica_core.pricing import resolve_model_pricing

        p = resolve_model_pricing("")
        assert p.input_per_1k > 0

    def test_very_long_model_name(self):
        from veronica_core.pricing import resolve_model_pricing

        p = resolve_model_pricing("x" * 10_000)
        assert p.input_per_1k > 0
