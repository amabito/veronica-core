"""Tests for veronica_core.adapters.langgraph — LangGraph node wrapper and callback.

Uses fake langgraph stubs injected into sys.modules so langgraph does not
need to be installed.
"""
from __future__ import annotations

import importlib
import math
import sys
import types
from typing import Callable

# ---------------------------------------------------------------------------
# Inject fake langgraph stubs BEFORE importing the adapter
# ---------------------------------------------------------------------------


def _build_fake_langgraph() -> None:
    """Create minimal langgraph stubs and register in sys.modules."""
    lg = types.ModuleType("langgraph")
    lg_graph = types.ModuleType("langgraph.graph")
    lg_prebuilt = types.ModuleType("langgraph.prebuilt")

    class FakeStateGraph:
        """Minimal StateGraph stand-in."""

        def __init__(self, schema: type) -> None:
            self.schema = schema
            self._nodes: dict = {}

        def add_node(self, name: str, fn: object) -> None:
            self._nodes[name] = fn

        def compile(self) -> "FakeStateGraph":
            return self

    lg_graph.StateGraph = FakeStateGraph
    lg.graph = lg_graph
    lg.prebuilt = lg_prebuilt

    sys.modules.update(
        {
            "langgraph": lg,
            "langgraph.graph": lg_graph,
            "langgraph.prebuilt": lg_prebuilt,
        }
    )


_build_fake_langgraph()

# ---------------------------------------------------------------------------
# Now safe to import the adapter (langgraph is already in sys.modules)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from veronica_core import GuardConfig  # noqa: E402
from veronica_core.adapters.langgraph import (  # noqa: E402
    VeronicaLangGraphCallback,
    veronica_node_wrapper,
)
from veronica_core.containment import ExecutionConfig  # noqa: E402
from veronica_core.inject import VeronicaHalt  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeLLMResult:
    """Minimal LLMResult stand-in for callback tests."""

    def __init__(self, llm_output=None) -> None:
        self.llm_output = llm_output


def _result(total_tokens: int = 100) -> FakeLLMResult:
    return FakeLLMResult(llm_output={"token_usage": {"total_tokens": total_tokens}})


def _result_no_usage() -> FakeLLMResult:
    return FakeLLMResult(llm_output=None)


def _make_node(return_value: dict | None = None) -> Callable[[dict], dict]:
    """Return a simple node function that returns the given state dict."""

    def node(state: dict) -> dict:
        return return_value if return_value is not None else state

    return node


# ---------------------------------------------------------------------------
# VeronicaLangGraphCallback — Allow path
# ---------------------------------------------------------------------------


class TestAllowPath:
    def test_on_llm_start_does_not_raise_within_limits(self) -> None:
        """on_llm_start: no exception when policies allow."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0, max_steps=5))
        cb.on_llm_start({}, ["Hello"])  # must not raise

    def test_on_llm_end_increments_step_counter(self) -> None:
        """on_llm_end: step_guard.current_step increments by 1."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_steps=10))
        assert cb.container.step_guard.current_step == 0
        cb.on_llm_end(_result())
        assert cb.container.step_guard.current_step == 1

    def test_on_llm_end_multiple_calls_accumulate_steps(self) -> None:
        """on_llm_end: multiple calls each increment step counter."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_steps=10))
        cb.on_llm_end(_result())
        cb.on_llm_end(_result())
        assert cb.container.step_guard.current_step == 2

    def test_on_llm_end_records_token_cost(self) -> None:
        """on_llm_end: budget.spend() is called with estimated cost.

        With no model name and total_tokens=1000, the adapter uses a 75/25
        input/output split and falls back to the unknown-model pricing
        (input=$0.030/1K, output=$0.060/1K):
          750 * 0.030/1000 + 250 * 0.060/1000 = 0.0225 + 0.0150 = 0.0375
        """
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0))
        cb.on_llm_end(_result(total_tokens=1000))
        assert cb.container.budget.call_count == 1
        assert cb.container.budget.spent_usd == pytest.approx(0.0375)

    def test_on_llm_end_zero_cost_when_no_usage(self) -> None:
        """on_llm_end: spend(0.0) called when llm_output has no usage."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0))
        cb.on_llm_end(_result_no_usage())
        assert cb.container.budget.spent_usd == 0.0

    def test_on_llm_error_does_not_raise(self) -> None:
        """on_llm_error: logs but does not raise or charge budget."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=1.0))
        cb.on_llm_error(RuntimeError("timeout"))  # must not raise
        assert cb.container.budget.spent_usd == 0.0


# ---------------------------------------------------------------------------
# VeronicaLangGraphCallback — Deny path
# ---------------------------------------------------------------------------


class TestDenyPath:
    def test_step_limit_raises_veronica_halt(self) -> None:
        """on_llm_start: raises VeronicaHalt when step limit is exhausted."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_steps=1))
        cb.on_llm_end(_result())  # step = 1
        with pytest.raises(VeronicaHalt, match="[Ss]tep"):
            cb.on_llm_start({}, ["Another"])

    def test_budget_exhausted_raises_veronica_halt(self) -> None:
        """on_llm_start: raises VeronicaHalt when budget is pre-exhausted."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=1.0))
        cb.container.budget.spend(2.0)  # exhaust manually
        with pytest.raises(VeronicaHalt, match="[Bb]udget"):
            cb.on_llm_start({}, ["Hello"])

    def test_veronica_halt_carries_decision(self) -> None:
        """VeronicaHalt from deny path carries a PolicyDecision."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=0.0))
        cb.container.budget.spend(1.0)
        with pytest.raises(VeronicaHalt) as exc_info:
            cb.on_llm_start({}, ["Hello"])
        assert exc_info.value.decision is not None
        assert not exc_info.value.decision.allowed


# ---------------------------------------------------------------------------
# Config acceptance
# ---------------------------------------------------------------------------


class TestConfigAcceptance:
    def test_callback_accepts_guard_config(self) -> None:
        """VeronicaLangGraphCallback accepts GuardConfig."""
        cfg = GuardConfig(max_cost_usd=5.0, max_steps=10, max_retries_total=3)
        cb = VeronicaLangGraphCallback(cfg)
        assert cb.container.budget.limit_usd == 5.0
        assert cb.container.step_guard.max_steps == 10

    def test_callback_accepts_execution_config(self) -> None:
        """VeronicaLangGraphCallback accepts ExecutionConfig."""
        cfg = ExecutionConfig(max_cost_usd=3.0, max_steps=15, max_retries_total=5)
        cb = VeronicaLangGraphCallback(cfg)
        assert cb.container.budget.limit_usd == 3.0
        assert cb.container.step_guard.max_steps == 15

    def test_wrapper_accepts_guard_config(self) -> None:
        """veronica_node_wrapper accepts GuardConfig."""
        cfg = GuardConfig(max_cost_usd=5.0, max_steps=10)
        wrapped = veronica_node_wrapper(cfg)(_make_node({}))
        assert wrapped.container.budget.limit_usd == 5.0
        assert wrapped.container.step_guard.max_steps == 10

    def test_wrapper_accepts_execution_config(self) -> None:
        """veronica_node_wrapper accepts ExecutionConfig."""
        cfg = ExecutionConfig(max_cost_usd=3.0, max_steps=15, max_retries_total=5)
        wrapped = veronica_node_wrapper(cfg)(_make_node({}))
        assert wrapped.container.budget.limit_usd == 3.0
        assert wrapped.container.step_guard.max_steps == 15


# ---------------------------------------------------------------------------
# Node wrapper — allow path
# ---------------------------------------------------------------------------


class TestNodeWrapper:
    def test_wrapped_node_executes_and_returns_state(self) -> None:
        """veronica_node_wrapper: node executes and returns state unchanged."""
        state = {"messages": ["hello"]}
        wrapped = veronica_node_wrapper(GuardConfig(max_steps=5))(_make_node(state))
        result = wrapped({"messages": []})
        assert result == state

    def test_wrapped_node_increments_step_on_success(self) -> None:
        """veronica_node_wrapper: step counter increments after successful node execution."""
        wrapped = veronica_node_wrapper(GuardConfig(max_steps=5))(_make_node({}))
        assert wrapped.container.step_guard.current_step == 0
        wrapped({})
        assert wrapped.container.step_guard.current_step == 1

    def test_wrapped_node_records_token_cost_from_state(self) -> None:
        """veronica_node_wrapper: cost is recorded when state contains token_usage."""
        state_with_usage = {"token_usage": {"total_tokens": 1000}}
        wrapped = veronica_node_wrapper(
            GuardConfig(max_cost_usd=10.0)
        )(_make_node(state_with_usage))
        wrapped({})
        # 1000 tokens: 750 in + 250 out → $0.0375 at unknown-model pricing
        assert wrapped.container.budget.spent_usd == pytest.approx(0.0375)

    def test_wrapped_node_zero_cost_when_no_usage_in_state(self) -> None:
        """veronica_node_wrapper: no budget spend when state has no token_usage."""
        wrapped = veronica_node_wrapper(GuardConfig(max_cost_usd=10.0))(_make_node({}))
        wrapped({})
        assert wrapped.container.budget.spent_usd == 0.0

    def test_wrapped_node_exposes_container(self) -> None:
        """veronica_node_wrapper: wrapped function exposes .container attribute."""
        wrapped = veronica_node_wrapper(GuardConfig(max_steps=5))(_make_node({}))
        assert hasattr(wrapped, "container")
        from veronica_core.container import AIContainer

        assert isinstance(wrapped.container, AIContainer)

    def test_wrapped_node_preserves_function_name(self) -> None:
        """veronica_node_wrapper: functools.wraps preserves __name__."""

        def my_special_node(state: dict) -> dict:
            return state

        wrapped = veronica_node_wrapper(GuardConfig(max_steps=5))(my_special_node)
        assert wrapped.__name__ == "my_special_node"

    def test_shared_container_across_nodes(self) -> None:
        """veronica_node_wrapper: shared container tracks steps across multiple nodes."""
        from veronica_core.container import AIContainer
        from veronica_core.agent_guard import AgentStepGuard
        from veronica_core.budget import BudgetEnforcer
        from veronica_core.retry import RetryContainer

        shared = AIContainer(
            budget=BudgetEnforcer(limit_usd=10.0),
            retry=RetryContainer(max_retries=5),
            step_guard=AgentStepGuard(max_steps=10),
        )
        cfg = GuardConfig(max_steps=10)
        node_a = veronica_node_wrapper(cfg, container=shared)(_make_node({}))
        node_b = veronica_node_wrapper(cfg, container=shared)(_make_node({}))

        node_a({})
        node_b({})
        # Both nodes share the same container; steps total = 2
        assert shared.step_guard.current_step == 2

    # ------------------------------------------------------------------
    # Deny path
    # ------------------------------------------------------------------

    def test_wrapped_node_raises_halt_when_step_limit_exhausted(self) -> None:
        """veronica_node_wrapper: VeronicaHalt raised pre-node when step limit hit."""
        wrapped = veronica_node_wrapper(GuardConfig(max_steps=1))(_make_node({}))
        wrapped({})  # step = 1, succeeds
        # Next call: step_guard sees current_step=1 >= max_steps=1 -> denied
        with pytest.raises(VeronicaHalt, match="[Ss]tep"):
            wrapped({})

    def test_wrapped_node_raises_halt_when_budget_exhausted(self) -> None:
        """veronica_node_wrapper: VeronicaHalt raised pre-node when budget exhausted."""
        wrapped = veronica_node_wrapper(GuardConfig(max_cost_usd=1.0))(_make_node({}))
        wrapped.container.budget.spend(2.0)  # exhaust manually
        with pytest.raises(VeronicaHalt, match="[Bb]udget"):
            wrapped({})

    def test_wrapped_node_halt_carries_decision(self) -> None:
        """VeronicaHalt from node wrapper carries a PolicyDecision."""
        wrapped = veronica_node_wrapper(GuardConfig(max_cost_usd=0.0))(_make_node({}))
        wrapped.container.budget.spend(1.0)
        with pytest.raises(VeronicaHalt) as exc_info:
            wrapped({})
        assert exc_info.value.decision is not None
        assert not exc_info.value.decision.allowed


# ---------------------------------------------------------------------------
# Import error when langgraph absent
# ---------------------------------------------------------------------------


class TestImportError:
    def test_raises_import_error_when_langgraph_absent(self) -> None:
        """Importing the adapter without langgraph raises a clear ImportError."""
        adapter_key = "veronica_core.adapters.langgraph"
        saved_adapter = sys.modules.pop(adapter_key, None)
        saved_lg = {k: v for k, v in sys.modules.items() if "langgraph" in k}
        for k in saved_lg:
            sys.modules.pop(k)

        try:
            with pytest.raises(ImportError, match="langgraph"):
                importlib.import_module("veronica_core.adapters.langgraph")
        finally:
            if saved_adapter is not None:
                sys.modules[adapter_key] = saved_adapter
            sys.modules.update(saved_lg)


# ---------------------------------------------------------------------------
# Adversarial tests — corrupted input, concurrent access, boundary abuse
# ---------------------------------------------------------------------------


class TestAdversarialLangGraph:
    """Adversarial tests for LangGraph adapter — attacker mindset."""

    # -- Corrupted input: garbage LLMResult objects --

    def test_on_llm_end_string_response_does_not_crash(self) -> None:
        """on_llm_end: string response must not crash."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0))
        cb.on_llm_end("not a real response")  # must not raise
        assert cb.container.budget.spent_usd == 0.0

    def test_on_llm_end_int_response_does_not_crash(self) -> None:
        """on_llm_end: int response must not crash."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0))
        cb.on_llm_end(42)  # must not raise

    def test_on_llm_end_none_llm_output_returns_zero(self) -> None:
        """on_llm_end: llm_output=None must not charge budget."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0))
        cb.on_llm_end(_result_no_usage())
        assert cb.container.budget.spent_usd == 0.0

    def test_on_llm_end_exploding_response(self) -> None:
        """on_llm_end: response that raises on attribute access must not crash."""

        class ExplodingResult:
            @property
            def llm_output(self):
                raise RuntimeError("I explode")

        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0))
        cb.on_llm_end(ExplodingResult())  # must not raise
        assert cb.container.budget.spent_usd == 0.0

    def test_on_llm_end_nan_tokens_does_not_produce_nan(self) -> None:
        """on_llm_end: NaN in token_usage must not produce NaN budget spend."""
        result = FakeLLMResult(llm_output={"token_usage": {"total_tokens": float("nan")}})
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0))
        cb.on_llm_end(result)
        assert not math.isnan(cb.container.budget.spent_usd)

    def test_on_llm_end_negative_tokens(self) -> None:
        """on_llm_end: negative total_tokens must not produce negative cost."""
        result = FakeLLMResult(llm_output={"token_usage": {"total_tokens": -100}})
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0))
        cb.on_llm_end(result)
        assert cb.container.budget.spent_usd >= 0.0

    def test_on_llm_end_string_tokens(self) -> None:
        """on_llm_end: string total_tokens must not crash."""
        result = FakeLLMResult(llm_output={"token_usage": {"total_tokens": "garbage"}})
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0))
        cb.on_llm_end(result)  # must not raise

    # -- Corrupted input: node wrapper with garbage state dicts --

    def test_node_wrapper_non_dict_return_does_not_crash(self) -> None:
        """veronica_node_wrapper: node returning non-dict must not crash cost extraction."""

        def string_node(state: dict) -> str:
            return "not a dict"

        wrapped = veronica_node_wrapper(GuardConfig(max_steps=5))(string_node)
        result = wrapped({})
        assert result == "not a dict"
        assert wrapped.container.budget.spent_usd == 0.0

    def test_node_wrapper_none_return_does_not_crash(self) -> None:
        """veronica_node_wrapper: node returning None must not crash."""

        def none_node(state: dict):
            return None

        wrapped = veronica_node_wrapper(GuardConfig(max_steps=5))(none_node)
        result = wrapped({})
        assert result is None

    def test_node_wrapper_nested_garbage_usage(self) -> None:
        """veronica_node_wrapper: garbage token_usage dict must not crash."""

        def garbage_node(state: dict) -> dict:
            return {"token_usage": "not a dict"}

        wrapped = veronica_node_wrapper(GuardConfig(max_cost_usd=10.0))(garbage_node)
        wrapped({})
        assert wrapped.container.budget.spent_usd == 0.0

    def test_node_wrapper_nan_total_tokens(self) -> None:
        """veronica_node_wrapper: NaN total_tokens in state must not propagate."""
        def nan_node(state: dict) -> dict:
            return {"token_usage": {"total_tokens": float("nan")}}

        wrapped = veronica_node_wrapper(GuardConfig(max_cost_usd=10.0))(nan_node)
        wrapped({})
        assert not math.isnan(wrapped.container.budget.spent_usd)

    # -- Concurrent access: multiple threads through callback --

    def test_concurrent_on_llm_start_near_step_limit(self) -> None:
        """on_llm_start: concurrent calls at step limit — all should deny or allow consistently."""
        import threading

        cb = VeronicaLangGraphCallback(GuardConfig(max_steps=1))
        cb.on_llm_end(_result())  # step = 1

        results = []
        errors = []

        def call_start():
            try:
                cb.on_llm_start({}, ["Hello"])
                results.append("allowed")
            except VeronicaHalt:
                results.append("denied")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=call_start) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Unexpected errors: {errors}"
        # All 10 should be denied (steps at limit)
        assert results.count("denied") == 10

    def test_concurrent_node_wrapper_budget_spend_thread_safe(self) -> None:
        """Node wrapper: concurrent execution must not corrupt budget total."""
        import threading

        from veronica_core.container import AIContainer
        from veronica_core.agent_guard import AgentStepGuard
        from veronica_core.budget import BudgetEnforcer
        from veronica_core.retry import RetryContainer

        shared = AIContainer(
            budget=BudgetEnforcer(limit_usd=100.0),
            retry=RetryContainer(max_retries=50),
            step_guard=AgentStepGuard(max_steps=100),
        )

        def cost_node(state: dict) -> dict:
            return {"token_usage": {"total_tokens": 1000}}

        wrapped = veronica_node_wrapper(
            GuardConfig(max_steps=100, max_cost_usd=100.0), container=shared
        )(cost_node)

        num_threads = 20
        threads = [threading.Thread(target=wrapped, args=({},)) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Steps must equal exactly num_threads
        assert shared.step_guard.current_step == num_threads

    # -- Boundary abuse: zero/extreme limits --

    def test_max_steps_zero_denies_immediately_callback(self) -> None:
        """on_llm_start with max_steps=0: deny on first call."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_steps=0))
        with pytest.raises(VeronicaHalt, match="[Ss]tep"):
            cb.on_llm_start({}, ["Hello"])

    def test_max_steps_zero_denies_immediately_wrapper(self) -> None:
        """Node wrapper with max_steps=0: deny on first call."""
        wrapped = veronica_node_wrapper(GuardConfig(max_steps=0))(_make_node({}))
        with pytest.raises(VeronicaHalt, match="[Ss]tep"):
            wrapped({})

    def test_max_cost_zero_denies_after_spend_callback(self) -> None:
        """on_llm_start with max_cost_usd=0.0: deny after any spend."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=0.0))
        cb.container.budget.spend(0.001)
        with pytest.raises(VeronicaHalt):
            cb.on_llm_start({}, ["Hello"])

    def test_huge_token_count_does_not_overflow(self) -> None:
        """on_llm_end: sys.maxsize tokens must not overflow or crash."""
        result = FakeLLMResult(llm_output={"token_usage": {"total_tokens": sys.maxsize}})
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0))
        cb.on_llm_end(result)  # must not raise

    def test_zero_total_tokens_returns_zero_cost(self) -> None:
        """on_llm_end: 0 total_tokens must not produce phantom cost."""
        result = FakeLLMResult(llm_output={"token_usage": {"total_tokens": 0}})
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0))
        cb.on_llm_end(result)
        assert cb.container.budget.spent_usd == 0.0

    # -- State corruption: continued use after deny --

    def test_on_llm_end_continues_counting_after_budget_exceeded(self) -> None:
        """Step counter continues even after budget is exceeded."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=0.001, max_steps=10))
        cb.container.budget.spend(1.0)
        assert cb.container.budget.is_exceeded

        # on_llm_end should still increment step
        cb.on_llm_end(_result())
        assert cb.container.step_guard.current_step == 1

    def test_on_llm_start_idempotent_after_deny(self) -> None:
        """on_llm_start: calling repeatedly after deny always raises."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_steps=0))
        for _ in range(5):
            with pytest.raises(VeronicaHalt):
                cb.on_llm_start({}, ["Hello"])

    def test_node_wrapper_idempotent_after_deny(self) -> None:
        """Node wrapper: calling repeatedly after deny always raises."""
        wrapped = veronica_node_wrapper(GuardConfig(max_steps=0))(_make_node({}))
        for _ in range(5):
            with pytest.raises(VeronicaHalt):
                wrapped({})

    # -- Partial failure: node raises exception --

    def test_node_wrapper_node_exception_does_not_increment_step(self) -> None:
        """veronica_node_wrapper: if node raises, step must NOT increment."""

        def failing_node(state: dict) -> dict:
            raise RuntimeError("Node crashed")

        wrapped = veronica_node_wrapper(GuardConfig(max_steps=5))(failing_node)
        with pytest.raises(RuntimeError, match="Node crashed"):
            wrapped({})
        # Step should NOT have incremented because node failed
        assert wrapped.container.step_guard.current_step == 0

    def test_on_llm_error_does_not_affect_state(self) -> None:
        """on_llm_error: must not modify budget or step counter."""
        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0, max_steps=10))
        cb.on_llm_error(RuntimeError("timeout"))
        cb.on_llm_error(ValueError("bad input"))
        cb.on_llm_error(ConnectionError("network"))
        assert cb.container.budget.spent_usd == 0.0
        assert cb.container.step_guard.current_step == 0
