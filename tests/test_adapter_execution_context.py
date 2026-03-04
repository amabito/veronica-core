"""Tests for ExecutionContext integration in framework adapters (Task #6, Item 2a).

Verifies that all 5 adapters (langchain, ag2, crewai, llamaindex, langgraph)
can accept an ExecutionContext and share budget tracking across components.
"""

from __future__ import annotations

import sys
import types


# ---------------------------------------------------------------------------
# Inject all required framework stubs before importing adapters
# ---------------------------------------------------------------------------


def _inject_all_stubs() -> dict:
    """Inject minimal stubs for all framework dependencies."""
    stubs: dict = {}

    # --- langchain ---
    lc_core = types.ModuleType("langchain_core")
    lc_callbacks = types.ModuleType("langchain_core.callbacks")
    lc_outputs = types.ModuleType("langchain_core.outputs")

    class FakeBaseCallbackHandler:
        def __init__(self) -> None:
            pass

    class FakeLLMResult:
        def __init__(self, llm_output=None) -> None:
            self.llm_output = llm_output

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
    stubs["FakeLLMResult"] = FakeLLMResult

    # --- ag2 ---
    ag2_mod = types.ModuleType("ag2")

    class FakeConversableAgent:
        def __init__(self, name: str = "agent", **kwargs) -> None:
            self.name = name
            self._reply_funcs: list = []

        def generate_reply(self, messages=None, sender=None, **kwargs):
            return "Hello from stub"

        def register_reply(self, trigger, reply_func, position: int = 0) -> None:
            self._reply_funcs.insert(position, (trigger, reply_func))

    ag2_mod.ConversableAgent = FakeConversableAgent
    sys.modules["ag2"] = ag2_mod
    stubs["FakeConversableAgent"] = FakeConversableAgent

    # --- langgraph ---
    lg = types.ModuleType("langgraph")
    lg_graph = types.ModuleType("langgraph.graph")
    lg_prebuilt = types.ModuleType("langgraph.prebuilt")
    lg_graph.StateGraph = object
    lg.graph = lg_graph
    lg.prebuilt = lg_prebuilt
    sys.modules.update(
        {
            "langgraph": lg,
            "langgraph.graph": lg_graph,
            "langgraph.prebuilt": lg_prebuilt,
        }
    )

    return stubs


_stubs = _inject_all_stubs()
FakeLLMResult = _stubs["FakeLLMResult"]
FakeConversableAgent = _stubs["FakeConversableAgent"]

# ---------------------------------------------------------------------------
# Now import adapters and veronica internals
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from veronica_core import GuardConfig  # noqa: E402
from veronica_core.adapters.ag2 import VeronicaConversableAgent  # noqa: E402
from veronica_core.adapters._shared import ExecutionContextContainerAdapter  # noqa: E402
from veronica_core.adapters.langchain import VeronicaCallbackHandler  # noqa: E402
from veronica_core.adapters.langgraph import (  # noqa: E402
    VeronicaLangGraphCallback,
    veronica_node_wrapper,
)
from veronica_core.containment import ExecutionConfig  # noqa: E402
from veronica_core.containment.execution_context import ExecutionContext  # noqa: E402
from veronica_core.inject import VeronicaHalt  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(max_cost_usd: float = 10.0, max_steps: int = 20) -> ExecutionContext:
    return ExecutionContext(
        config=ExecutionConfig(
            max_cost_usd=max_cost_usd,
            max_steps=max_steps,
            max_retries_total=5,
        )
    )


def _llm_result(total_tokens: int = 100) -> FakeLLMResult:
    return FakeLLMResult(llm_output={"token_usage": {"total_tokens": total_tokens}})


# ---------------------------------------------------------------------------
# Test: ExecutionContext accepted by all adapters
# ---------------------------------------------------------------------------


class TestAdaptersAcceptExecutionContext:
    """All 5 adapters must accept execution_context= parameter."""

    def test_langchain_accepts_execution_context(self) -> None:
        ctx = _make_ctx()
        handler = VeronicaCallbackHandler(
            GuardConfig(max_cost_usd=10.0, max_steps=20),
            execution_context=ctx,
        )
        assert isinstance(handler._container, ExecutionContextContainerAdapter)

    def test_ag2_accepts_execution_context(self) -> None:
        ctx = _make_ctx()
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_cost_usd=10.0, max_steps=20),
            execution_context=ctx,
        )
        assert isinstance(agent._container, ExecutionContextContainerAdapter)

    def test_langgraph_callback_accepts_execution_context(self) -> None:
        ctx = _make_ctx()
        cb = VeronicaLangGraphCallback(
            GuardConfig(max_cost_usd=10.0, max_steps=20),
            execution_context=ctx,
        )
        assert isinstance(cb._container, ExecutionContextContainerAdapter)

    def test_langgraph_node_wrapper_accepts_execution_context(self) -> None:
        ctx = _make_ctx()
        wrapped = veronica_node_wrapper(
            GuardConfig(max_cost_usd=10.0, max_steps=20),
            execution_context=ctx,
        )(lambda state: state)
        assert isinstance(wrapped.container, ExecutionContextContainerAdapter)


# ---------------------------------------------------------------------------
# Test: Standalone (no ExecutionContext) still returns AIContainer
# ---------------------------------------------------------------------------


class TestStandaloneContainerIsAIContainer:
    """Without execution_context, adapters return AIContainer (backward compat)."""

    def test_langchain_standalone_returns_aicontainer(self) -> None:
        from veronica_core.container import AIContainer

        handler = VeronicaCallbackHandler(GuardConfig(max_cost_usd=10.0, max_steps=20))
        assert isinstance(handler.container, AIContainer)

    def test_ag2_standalone_returns_aicontainer(self) -> None:
        from veronica_core.container import AIContainer

        agent = VeronicaConversableAgent(
            "assistant", config=GuardConfig(max_cost_usd=10.0, max_steps=20)
        )
        assert isinstance(agent.container, AIContainer)

    def test_langgraph_callback_standalone_returns_aicontainer(self) -> None:
        from veronica_core.container import AIContainer

        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=10.0, max_steps=20))
        assert isinstance(cb.container, AIContainer)


# ---------------------------------------------------------------------------
# Test: ExecutionContext step tracking
# ---------------------------------------------------------------------------


class TestExecutionContextStepTracking:
    """Steps tracked through adapter must be visible in ExecutionContext snapshot."""

    def test_langchain_steps_via_execution_context(self) -> None:
        ctx = _make_ctx(max_steps=20)
        handler = VeronicaCallbackHandler(
            GuardConfig(max_cost_usd=10.0, max_steps=20),
            execution_context=ctx,
        )
        # Step counter starts at 0
        assert handler.container.step_guard.current_step == 0
        handler.on_llm_end(_llm_result())
        assert handler.container.step_guard.current_step == 1
        handler.on_llm_end(_llm_result())
        assert handler.container.step_guard.current_step == 2

    def test_ag2_steps_via_execution_context(self) -> None:
        ctx = _make_ctx(max_steps=20)
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_steps=20),
            execution_context=ctx,
        )
        assert agent.container.step_guard.current_step == 0
        agent.generate_reply(messages=[{"role": "user", "content": "hi"}])
        assert agent.container.step_guard.current_step == 1

    def test_langgraph_callback_steps_via_execution_context(self) -> None:
        ctx = _make_ctx(max_steps=20)
        cb = VeronicaLangGraphCallback(
            GuardConfig(max_steps=20),
            execution_context=ctx,
        )
        assert cb.container.step_guard.current_step == 0
        cb.on_llm_end(_llm_result())
        assert cb.container.step_guard.current_step == 1


# ---------------------------------------------------------------------------
# Test: ExecutionContext budget tracking
# ---------------------------------------------------------------------------


class TestExecutionContextBudgetTracking:
    """Budget spent through adapter must be reflected in container.budget."""

    def test_langchain_budget_via_execution_context(self) -> None:
        ctx = _make_ctx(max_cost_usd=10.0)
        handler = VeronicaCallbackHandler(
            GuardConfig(max_cost_usd=10.0, max_steps=20),
            execution_context=ctx,
        )
        assert handler.container.budget.limit_usd == 10.0
        handler.on_llm_end(_llm_result(total_tokens=1000))
        # Cost should be positive (1000 tokens at unknown-model pricing)
        assert handler.container.budget.spent_usd > 0.0

    def test_langgraph_budget_via_execution_context(self) -> None:
        ctx = _make_ctx(max_cost_usd=10.0)
        cb = VeronicaLangGraphCallback(
            GuardConfig(max_cost_usd=10.0, max_steps=20),
            execution_context=ctx,
        )
        cb.on_llm_end(_llm_result(total_tokens=1000))
        assert cb.container.budget.spent_usd > 0.0


# ---------------------------------------------------------------------------
# Test: Policy deny with ExecutionContext
# ---------------------------------------------------------------------------


class TestExecutionContextPolicyDeny:
    """Adapters with execution_context must enforce policy limits."""

    def test_langchain_step_limit_with_execution_context(self) -> None:
        ctx = _make_ctx(max_steps=1)
        handler = VeronicaCallbackHandler(
            GuardConfig(max_cost_usd=10.0, max_steps=1),
            execution_context=ctx,
        )
        handler.on_llm_end(_llm_result())  # step = 1
        with pytest.raises(VeronicaHalt, match="[Ss]tep"):
            handler.on_llm_start({}, ["hello"])

    def test_ag2_step_limit_with_execution_context(self) -> None:
        ctx = _make_ctx(max_steps=1)
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_steps=1),
            execution_context=ctx,
        )
        # Manually increment to reach limit
        agent.container.step_guard.step()
        with pytest.raises(VeronicaHalt, match="[Ss]tep"):
            agent.generate_reply(messages=[])


# ---------------------------------------------------------------------------
# Test: ExecutionContextContainerAdapter introspection API
# ---------------------------------------------------------------------------


class TestExecutionContextContainerAdapterAPI:
    """ExecutionContextContainerAdapter must expose AIContainer-compatible API."""

    def test_budget_limit_usd(self) -> None:
        ctx = _make_ctx(max_cost_usd=5.0)
        adapter = ExecutionContextContainerAdapter(
            ctx, GuardConfig(max_cost_usd=5.0, max_steps=10)
        )
        assert adapter.budget.limit_usd == 5.0

    def test_step_guard_max_steps(self) -> None:
        ctx = _make_ctx(max_steps=15)
        adapter = ExecutionContextContainerAdapter(
            ctx, GuardConfig(max_cost_usd=5.0, max_steps=15)
        )
        assert adapter.step_guard.max_steps == 15

    def test_step_guard_current_step_initial(self) -> None:
        ctx = _make_ctx()
        adapter = ExecutionContextContainerAdapter(
            ctx, GuardConfig(max_cost_usd=5.0, max_steps=10)
        )
        assert adapter.step_guard.current_step == 0

    def test_budget_is_exceeded_false_initially(self) -> None:
        ctx = _make_ctx(max_cost_usd=5.0)
        adapter = ExecutionContextContainerAdapter(
            ctx, GuardConfig(max_cost_usd=5.0, max_steps=10)
        )
        assert adapter.budget.is_exceeded is False

    def test_check_allows_within_limits(self) -> None:
        ctx = _make_ctx(max_cost_usd=5.0, max_steps=10)
        adapter = ExecutionContextContainerAdapter(
            ctx, GuardConfig(max_cost_usd=5.0, max_steps=10)
        )
        decision = adapter.check(cost_usd=0.0)
        assert decision.allowed is True

    def test_check_denies_on_step_limit(self) -> None:
        ctx = _make_ctx(max_cost_usd=5.0, max_steps=0)
        adapter = ExecutionContextContainerAdapter(
            ctx, GuardConfig(max_cost_usd=5.0, max_steps=0)
        )
        decision = adapter.check(cost_usd=0.0)
        assert decision.allowed is False
        assert "step" in decision.reason.lower() or "Step" in decision.reason


# ---------------------------------------------------------------------------
# Test: Shared budget across two adapters on same ExecutionContext
# ---------------------------------------------------------------------------


class TestSharedBudgetAcrossAdapters:
    """Two adapters sharing one ExecutionContext must share budget tracking."""

    def test_two_langchain_handlers_share_steps(self) -> None:
        ctx = _make_ctx(max_steps=20)
        config = GuardConfig(max_cost_usd=10.0, max_steps=20)

        handler_a = VeronicaCallbackHandler(config, execution_context=ctx)
        handler_b = VeronicaCallbackHandler(config, execution_context=ctx)

        handler_a.on_llm_end(_llm_result())
        handler_b.on_llm_end(_llm_result())

        # Both adapters share the same ctx._step_count
        assert handler_a.container.step_guard.current_step == 2
        assert handler_b.container.step_guard.current_step == 2
