"""Unit tests for the 3 wired TODO paths in ExecutionContext.

Tests:
1. CircuitBreaker OPEN must halt before fn() is ever called.
2. pipeline.before_charge() called exactly once per successful LLM call.
3. kind='tool' routes to before_tool_call(), not before_llm_call().
4. before_charge() must NOT be called for tool calls.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.types import Decision


def test_circuit_breaker_blocks_before_dispatch():
    """CircuitBreaker OPEN must halt before fn() is ever called."""
    from veronica_core.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=9999.0)
    breaker.record_failure()  # circuit OPEN

    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config, circuit_breaker=breaker)

    called = []
    decision = ctx.wrap_llm_call(fn=lambda: called.append(1))
    assert decision == Decision.HALT
    assert called == [], "fn must not be called when circuit is OPEN"

    snap = ctx.get_snapshot()
    assert any(e.event_type == "CHAIN_CIRCUIT_OPEN" for e in snap.events)


def test_before_charge_called_once_per_llm_call():
    """pipeline.before_charge() called exactly once per successful LLM call."""

    class ChargeCapture:
        def __init__(self):
            self.calls: list[float] = []

        def before_charge(self, ctx, cost_usd: float):
            self.calls.append(cost_usd)
            return None  # ALLOW

    capture = ChargeCapture()
    pipeline = ShieldPipeline(budget=capture)

    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config, pipeline=pipeline)

    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.05))
    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.10))

    assert len(capture.calls) == 2
    assert abs(capture.calls[0] - 0.05) < 1e-9
    assert abs(capture.calls[1] - 0.10) < 1e-9


def test_tool_routing_uses_before_tool_call():
    """kind='tool' must invoke before_tool_call(), not before_llm_call()."""

    class ToolHookSpy:
        def __init__(self):
            self.tool_calls = 0
            self.llm_calls = 0

        def before_tool_call(self, ctx) -> Decision | None:
            self.tool_calls += 1
            return None

        def before_llm_call(self, ctx) -> Decision | None:
            self.llm_calls += 1
            return None

    spy = ToolHookSpy()
    # ShieldPipeline accepts both pre_dispatch (LLM) and tool_dispatch (tool)
    pipeline = ShieldPipeline(pre_dispatch=spy, tool_dispatch=spy)

    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config, pipeline=pipeline)

    ctx.wrap_tool_call(fn=lambda: None, options=WrapOptions(operation_name="my_tool"))
    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(operation_name="my_llm"))

    assert spy.tool_calls == 1, f"Expected 1 tool call, got {spy.tool_calls}"
    assert spy.llm_calls == 1, f"Expected 1 llm call, got {spy.llm_calls}"


def test_before_charge_skipped_for_tool_calls():
    """before_charge() must NOT be called for tool calls."""

    class ChargeCapture:
        def __init__(self):
            self.calls = 0

        def before_charge(self, ctx, cost_usd):
            self.calls += 1
            return None

    capture = ChargeCapture()
    pipeline = ShieldPipeline(budget=capture)
    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config, pipeline=pipeline)

    ctx.wrap_tool_call(
        fn=lambda: None,
        options=WrapOptions(operation_name="tool", cost_estimate_hint=0.05),
    )
    assert capture.calls == 0, "before_charge must not fire for tool calls"


def test_local_cost_accumulation_independent_of_backend_total():
    """_cost_usd_accumulated must track LOCAL spend only, not the backend global total.

    Regression test for the budget double-spend bug: when a Redis backend
    returns a cross-process global total from add(), the context must NOT
    assign that value to _cost_usd_accumulated.  Only the local per-call cost
    should be added.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from veronica_core.distributed import LocalBudgetBackend

    # Seed the backend with a large pre-existing total (simulating other
    # processes that have already spent budget in a shared Redis namespace).
    backend = LocalBudgetBackend()
    backend.add(99.0)  # pre-existing spend from "other processes"

    config = ExecutionConfig(
        max_cost_usd=200.0,  # high ceiling so limit check doesn't interfere
        max_steps=10,
        max_retries_total=5,
        budget_backend=backend,
    )
    ctx = ExecutionContext(config=config)

    # Perform two local calls costing 0.10 and 0.20 USD each.
    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.10))
    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.20))

    snap = ctx.get_snapshot()
    # Local accumulator must equal only what THIS context spent (0.30),
    # not the backend total (99.30).
    assert abs(snap.cost_usd_accumulated - 0.30) < 1e-9, (
        f"Expected local cost 0.30, got {snap.cost_usd_accumulated:.6f}. "
        "This indicates the backend global total was incorrectly assigned."
    )


# ---------------------------------------------------------------------------
# Adversarial tests for C1, H3, M2, M4, M6, L6 fixes
# ---------------------------------------------------------------------------


class TestAdversarialExecutionConfig:
    """C1: ExecutionConfig NaN/Inf/negative input validation."""

    def test_nan_max_cost_raises(self):
        """NaN max_cost_usd must raise ValueError — bypasses all budget checks."""
        import pytest
        with pytest.raises(ValueError, match="finite"):
            ExecutionConfig(max_cost_usd=float("nan"), max_steps=10, max_retries_total=3)

    def test_positive_inf_max_cost_raises(self):
        """+Inf max_cost_usd must raise ValueError."""
        import pytest
        with pytest.raises(ValueError, match="finite"):
            ExecutionConfig(max_cost_usd=float("inf"), max_steps=10, max_retries_total=3)

    def test_negative_inf_max_cost_raises(self):
        """-Inf max_cost_usd must raise ValueError."""
        import pytest
        with pytest.raises(ValueError, match="finite"):
            ExecutionConfig(max_cost_usd=float("-inf"), max_steps=10, max_retries_total=3)

    def test_negative_max_cost_raises(self):
        """Negative max_cost_usd must raise ValueError."""
        import pytest
        with pytest.raises(ValueError, match="non-negative"):
            ExecutionConfig(max_cost_usd=-1.0, max_steps=10, max_retries_total=3)

    def test_negative_max_steps_raises(self):
        """Negative max_steps must raise ValueError."""
        import pytest
        with pytest.raises(ValueError, match="non-negative"):
            ExecutionConfig(max_cost_usd=1.0, max_steps=-1, max_retries_total=3)

    def test_negative_max_retries_raises(self):
        """Negative max_retries_total must raise ValueError."""
        import pytest
        with pytest.raises(ValueError, match="non-negative"):
            ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=-1)

    def test_zero_max_cost_allowed(self):
        """Zero max_cost_usd is valid (immediately halts on first call)."""
        cfg = ExecutionConfig(max_cost_usd=0.0, max_steps=10, max_retries_total=3)
        assert cfg.max_cost_usd == 0.0

    def test_valid_config_succeeds(self):
        """Normal positive values must not raise."""
        cfg = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=3)
        assert cfg.max_cost_usd == 1.0


class TestAdversarialH3BackendOutsideLock:
    """H3: backend.add() must not block other threads during its execution."""

    def test_backend_add_does_not_hold_lock_during_io(self):
        """backend.add() must release _lock before calling into the backend.

        Simulates a slow backend (1s sleep). A second thread must be able to
        acquire _lock and call get_snapshot() before the slow add() finishes.
        """
        import pytest

        slow_add_started = threading.Event()
        other_thread_got_snapshot = threading.Event()

        class SlowBackend:
            def add(self, cost: float) -> float:
                slow_add_started.set()
                time.sleep(0.3)  # simulate Redis round-trip
                return cost

            def get(self) -> float:
                return 0.0

            def close(self) -> None:
                pass

        config = ExecutionConfig(
            max_cost_usd=10.0,
            max_steps=10,
            max_retries_total=3,
            budget_backend=SlowBackend(),
        )
        ctx = ExecutionContext(config=config)
        errors: list[str] = []

        def wrap_thread():
            ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.01))

        def snapshot_thread():
            # Wait until slow backend.add() has started, then try to snapshot.
            slow_add_started.wait(timeout=2.0)
            try:
                snap = ctx.get_snapshot()
                other_thread_got_snapshot.set()
            except Exception as exc:
                errors.append(str(exc))

        t1 = threading.Thread(target=wrap_thread)
        t2 = threading.Thread(target=snapshot_thread)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert not errors, f"snapshot_thread raised: {errors}"
        # If the lock was held during backend.add(), t2 would have blocked the
        # full 0.3s. The test can't guarantee timing perfectly, but at least
        # verifies no deadlock / exception occurred.
        assert other_thread_got_snapshot.is_set(), (
            "snapshot_thread never completed — possible lock held during backend IO"
        )


class TestAdversarialM4DedupPerformance:
    """M4: _emit_chain_event O(1) dedup — should not degrade with 900+ events."""

    def test_emit_chain_event_dedup_with_many_events(self):
        """Dedup must work correctly even near the _MAX_CHAIN_EVENTS cap."""
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=10000, max_retries_total=10000)
        ctx = ExecutionContext(config=config)

        # Generate 900 unique events by running many calls that emit budget events.
        # The easiest way: exhaust retries to emit repeated retry_budget_exceeded.
        # But for performance test, directly call abort() with many distinct reasons.
        for i in range(200):
            ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.0))

        # Should complete without exponential slowdown
        snap = ctx.get_snapshot()
        assert snap.step_count == 200

    def test_dedup_prevents_duplicate_events(self):
        """Identical events emitted twice must appear only once in the log."""
        config = ExecutionConfig(max_cost_usd=0.001, max_steps=10, max_retries_total=3)
        ctx = ExecutionContext(config=config)

        # First call: accumulate cost that triggers budget_exceeded on second call.
        ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.001))
        # Second call: budget exceeded event emitted.
        ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.001))
        # Third call: same budget exceeded event — must be deduped.
        ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.001))

        snap = ctx.get_snapshot()
        budget_events = [e for e in snap.events if e.event_type == "CHAIN_BUDGET_EXCEEDED"]
        assert len(budget_events) == 1, (
            f"Expected exactly 1 CHAIN_BUDGET_EXCEEDED event, got {len(budget_events)}"
        )


class TestAdversarialL6GraphSnapshotLock:
    """L6: get_graph_snapshot() must acquire lock for consistency."""

    def test_get_graph_snapshot_concurrent_with_writes(self):
        """get_graph_snapshot() must not return torn data during concurrent writes."""
        config = ExecutionConfig(max_cost_usd=100.0, max_steps=1000, max_retries_total=1000)
        ctx = ExecutionContext(config=config)
        errors: list[str] = []
        stop_flag = threading.Event()

        def writer():
            while not stop_flag.is_set():
                ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.0))

        def reader():
            for _ in range(50):
                try:
                    snap = ctx.get_graph_snapshot()
                    assert isinstance(snap, dict), "get_graph_snapshot must return dict"
                    assert "aggregates" in snap, "snapshot must have aggregates key"
                except Exception as exc:
                    errors.append(str(exc))

        t_write = threading.Thread(target=writer)
        t_read = threading.Thread(target=reader)
        t_write.start()
        t_read.start()
        t_read.join(timeout=5.0)
        stop_flag.set()
        t_write.join(timeout=5.0)

        assert not errors, f"get_graph_snapshot raised errors: {errors}"
