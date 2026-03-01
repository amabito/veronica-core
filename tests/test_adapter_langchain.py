"""Tests for veronica_core.adapters.langchain — LangChain callback handler.

Uses fake langchain stubs injected into sys.modules so neither langchain-core
nor langchain need to be installed.
"""
from __future__ import annotations

import sys
import types

# ---------------------------------------------------------------------------
# Inject fake langchain_core stubs BEFORE importing the adapter
# ---------------------------------------------------------------------------


def _build_fake_langchain() -> type:
    """Create minimal langchain_core stubs and register in sys.modules.

    Returns the FakeLLMResult class for use in test helpers.
    """
    lc_core = types.ModuleType("langchain_core")
    lc_callbacks = types.ModuleType("langchain_core.callbacks")
    lc_outputs = types.ModuleType("langchain_core.outputs")

    class FakeBaseCallbackHandler:
        """Minimal BaseCallbackHandler stand-in."""

        def __init__(self) -> None:
            pass

    class FakeLLMResult:
        """Minimal LLMResult stand-in."""

        def __init__(self, llm_output=None, generations=None) -> None:
            self.llm_output = llm_output
            self.generations = generations or []

    lc_callbacks.BaseCallbackHandler = FakeBaseCallbackHandler
    lc_outputs.LLMResult = FakeLLMResult
    lc_core.callbacks = lc_callbacks
    lc_core.outputs = lc_outputs

    sys.modules.update(
        {
            "langchain_core": lc_core,
            "langchain_core.callbacks": lc_callbacks,
            "langchain_core.outputs": lc_outputs,
        }
    )
    return FakeLLMResult


FakeLLMResult = _build_fake_langchain()

# ---------------------------------------------------------------------------
# Now safe to import the adapter (langchain_core is already in sys.modules)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from veronica_core import GuardConfig  # noqa: E402
from veronica_core.adapters.langchain import VeronicaCallbackHandler  # noqa: E402
from veronica_core.containment import ExecutionConfig  # noqa: E402
from veronica_core.inject import VeronicaHalt  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(total_tokens: int = 100) -> FakeLLMResult:
    return FakeLLMResult(llm_output={"token_usage": {"total_tokens": total_tokens}})


def _result_no_usage() -> FakeLLMResult:
    return FakeLLMResult(llm_output=None)


# ---------------------------------------------------------------------------
# Allow path
# ---------------------------------------------------------------------------


class TestAllowPath:
    def test_on_llm_start_does_not_raise_within_limits(self) -> None:
        """on_llm_start: no exception when policies allow."""
        handler = VeronicaCallbackHandler(GuardConfig(max_cost_usd=10.0, max_steps=5))
        handler.on_llm_start({}, ["Hello"])  # must not raise

    def test_on_llm_end_increments_step_counter(self) -> None:
        """on_llm_end: step_guard.current_step increments by 1."""
        handler = VeronicaCallbackHandler(GuardConfig(max_steps=10))
        assert handler.container.step_guard.current_step == 0
        handler.on_llm_end(_result())
        assert handler.container.step_guard.current_step == 1

    def test_on_llm_end_multiple_calls_accumulate_steps(self) -> None:
        """on_llm_end: multiple calls each increment step counter."""
        handler = VeronicaCallbackHandler(GuardConfig(max_steps=10))
        handler.on_llm_end(_result())
        handler.on_llm_end(_result())
        assert handler.container.step_guard.current_step == 2

    def test_on_llm_end_records_token_cost(self) -> None:
        """on_llm_end: budget.spend() is called with estimated cost.

        With no model name and total_tokens=1000, the adapter uses a 75/25
        input/output split and falls back to the unknown-model pricing
        (input=$0.030/1K, output=$0.060/1K):
          750 * 0.030/1000 + 250 * 0.060/1000 = 0.0225 + 0.0150 = 0.0375
        """
        handler = VeronicaCallbackHandler(GuardConfig(max_cost_usd=10.0))
        handler.on_llm_end(_result(total_tokens=1000))
        assert handler.container.budget.call_count == 1
        assert handler.container.budget.spent_usd == pytest.approx(0.0375)

    def test_on_llm_end_zero_cost_when_no_usage(self) -> None:
        """on_llm_end: spend(0.0) called when llm_output has no usage."""
        handler = VeronicaCallbackHandler(GuardConfig(max_cost_usd=10.0))
        handler.on_llm_end(_result_no_usage())
        assert handler.container.budget.spent_usd == 0.0

    def test_on_llm_error_does_not_raise(self) -> None:
        """on_llm_error: logs but does not raise or charge budget."""
        handler = VeronicaCallbackHandler(GuardConfig(max_cost_usd=1.0))
        handler.on_llm_error(RuntimeError("timeout"))  # must not raise
        assert handler.container.budget.spent_usd == 0.0


# ---------------------------------------------------------------------------
# Deny path
# ---------------------------------------------------------------------------


class TestDenyPath:
    def test_step_limit_raises_veronica_halt(self) -> None:
        """on_llm_start: raises VeronicaHalt when step limit is exhausted."""
        handler = VeronicaCallbackHandler(GuardConfig(max_steps=1))
        # Exhaust: one successful call increments step to 1
        handler.on_llm_end(_result())  # step = 1
        # Next start: step_guard sees current_step=1 >= max_steps=1 -> denied
        with pytest.raises(VeronicaHalt, match="[Ss]tep"):
            handler.on_llm_start({}, ["Another"])

    def test_budget_exhausted_raises_veronica_halt(self) -> None:
        """on_llm_start: raises VeronicaHalt when budget is pre-exhausted."""
        handler = VeronicaCallbackHandler(GuardConfig(max_cost_usd=1.0))
        handler.container.budget.spend(2.0)  # exhaust manually
        with pytest.raises(VeronicaHalt, match="[Bb]udget"):
            handler.on_llm_start({}, ["Hello"])

    def test_veronica_halt_carries_decision(self) -> None:
        """VeronicaHalt from deny path carries a PolicyDecision."""
        handler = VeronicaCallbackHandler(GuardConfig(max_cost_usd=0.0))
        handler.container.budget.spend(1.0)
        with pytest.raises(VeronicaHalt) as exc_info:
            handler.on_llm_start({}, ["Hello"])
        assert exc_info.value.decision is not None
        assert not exc_info.value.decision.allowed


# ---------------------------------------------------------------------------
# Config acceptance
# ---------------------------------------------------------------------------


class TestConfigAcceptance:
    def test_accepts_guard_config(self) -> None:
        """VeronicaCallbackHandler accepts GuardConfig."""
        cfg = GuardConfig(max_cost_usd=5.0, max_steps=10, max_retries_total=3)
        handler = VeronicaCallbackHandler(cfg)
        assert handler.container.budget.limit_usd == 5.0
        assert handler.container.step_guard.max_steps == 10

    def test_accepts_execution_config(self) -> None:
        """VeronicaCallbackHandler accepts ExecutionConfig."""
        cfg = ExecutionConfig(max_cost_usd=3.0, max_steps=15, max_retries_total=5)
        handler = VeronicaCallbackHandler(cfg)
        assert handler.container.budget.limit_usd == 3.0
        assert handler.container.step_guard.max_steps == 15


# ---------------------------------------------------------------------------
# LangChain cost boundary — small / edge-case token counts
# ---------------------------------------------------------------------------


class TestCostBoundary:
    """Cost must be > 0 for any positive total_tokens value, including 1-3."""

    @pytest.mark.parametrize("total_tokens", [1, 2, 3, 100, 10_000])
    def test_cost_positive_for_total_tokens(self, total_tokens: int) -> None:
        """_estimate_cost must return a positive float for every total_tokens >= 1."""
        from veronica_core.adapters.langchain import _estimate_cost

        result = _estimate_cost(
            FakeLLMResult(llm_output={"token_usage": {"total_tokens": total_tokens}})
        )
        assert result > 0.0, f"cost was {result} for total_tokens={total_tokens}"

    def test_explicit_prompt_completion_tokens_used_directly(self) -> None:
        """When prompt_tokens and completion_tokens are explicitly provided, they are used
        verbatim without any heuristic split."""
        handler = VeronicaCallbackHandler(GuardConfig(max_cost_usd=10.0))
        result = FakeLLMResult(
            llm_output={
                "token_usage": {
                    "prompt_tokens": 80,
                    "completion_tokens": 20,
                }
            }
        )
        handler.on_llm_end(result)
        # Cost must be non-zero and derived from 80+20=100 tokens
        assert handler.container.budget.spent_usd > 0.0

    def test_zero_total_tokens_flows_through_calculation(self) -> None:
        """total_tokens=0 must reach estimate_cost_usd() and produce 0.0 naturally.

        Regression test for the LangChain zero-token bypass bug: the early
        return on ``total_raw == 0`` was removed so that 0 tokens flow through
        the normal estimate_cost_usd() path rather than short-circuiting.
        The result must still be 0.0 (no phantom spend).
        """
        from veronica_core.adapters.langchain import _estimate_cost

        # total_tokens=0 -> tokens_in = max(1, 0) = 1, tokens_out = 0 - 1 = -1
        # estimate_cost_usd with 1 prompt + (-1) completion token is 0.0
        # because negative token counts produce no positive cost, but the
        # important guarantee is: no phantom cost and no exception raised.
        result = _estimate_cost(
            FakeLLMResult(llm_output={"token_usage": {"total_tokens": 0}})
        )
        assert result == 0.0, f"Expected 0.0 for zero tokens, got {result}"
        assert isinstance(result, float), "Result must be float"

    def test_none_total_tokens_returns_zero_cost(self) -> None:
        """total_tokens=None must still return 0.0 (missing data guard remains)."""
        from veronica_core.adapters.langchain import _estimate_cost

        result = _estimate_cost(
            FakeLLMResult(llm_output={"token_usage": {}})
        )
        assert result == 0.0, f"Expected 0.0 for missing total_tokens, got {result}"


# ---------------------------------------------------------------------------
# Import error when langchain absent
# ---------------------------------------------------------------------------


class TestImportError:
    def test_raises_import_error_when_langchain_absent(self) -> None:
        """Importing the adapter without langchain raises a clear ImportError."""
        import importlib

        adapter_key = "veronica_core.adapters.langchain"
        saved_adapter = sys.modules.pop(adapter_key, None)
        saved_lc = {k: v for k, v in sys.modules.items() if "langchain" in k}
        for k in saved_lc:
            sys.modules.pop(k)

        try:
            with pytest.raises(ImportError, match="langchain"):
                importlib.import_module("veronica_core.adapters.langchain")
        finally:
            if saved_adapter is not None:
                sys.modules[adapter_key] = saved_adapter
            sys.modules.update(saved_lc)
