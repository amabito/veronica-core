"""Tests for ContainmentMetricsProtocol wiring in framework adapters.

Verifies that record_decision and record_tokens are emitted correctly
by langchain, crewai, langgraph, llamaindex, and ag2 adapters.

Uses fake framework stubs to avoid installing optional dependencies.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from veronica_core.adapters._shared import safe_emit
from veronica_core.inject import GuardConfig, VeronicaHalt


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_metrics() -> MagicMock:
    """Return a fresh mock ContainmentMetricsProtocol."""
    m = MagicMock()
    m.record_decision = MagicMock()
    m.record_tokens = MagicMock()
    m.record_cost = MagicMock()
    return m


def _unlimited_config() -> GuardConfig:
    return GuardConfig(max_cost_usd=100.0, max_steps=100, max_retries_total=5)


def _exhausted_config() -> GuardConfig:
    return GuardConfig(max_cost_usd=0.0, max_steps=0, max_retries_total=0)


# ---------------------------------------------------------------------------
# Tests for _shared helpers
# ---------------------------------------------------------------------------


class TestSafeEmit:
    def test_none_metrics_is_noop(self) -> None:
        safe_emit(None, "record_decision", "a", "ALLOW")

    def test_missing_method_is_noop(self) -> None:
        obj = MagicMock(spec=[])  # no methods
        safe_emit(obj, "record_decision", "a", "ALLOW")  # must not raise

    def test_calls_correct_method(self) -> None:
        m = _make_metrics()
        safe_emit(m, "record_decision", "agent", "HALT")
        m.record_decision.assert_called_once_with("agent", "HALT")

    def test_exception_swallowed(self) -> None:
        m = _make_metrics()
        m.record_decision.side_effect = ValueError("boom")
        safe_emit(m, "record_decision", "a", "ALLOW")  # must not raise


# ---------------------------------------------------------------------------
# LangChain adapter metrics tests
# ---------------------------------------------------------------------------


def _build_fake_langchain_stubs() -> Any:
    """Inject minimal langchain_core stubs and return FakeLLMResult class."""
    if "langchain_core" in sys.modules:
        return sys.modules["langchain_core.outputs"].LLMResult

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
    return FakeLLMResult


class TestLangChainAdapterMetrics:
    def setup_method(self) -> None:
        self.FakeLLMResult = _build_fake_langchain_stubs()
        # Force re-import to pick up stubs
        if "veronica_core.adapters.langchain" in sys.modules:
            del sys.modules["veronica_core.adapters.langchain"]
        from veronica_core.adapters.langchain import VeronicaCallbackHandler

        self.HandlerClass = VeronicaCallbackHandler

    def test_allow_emits_record_decision(self) -> None:
        m = _make_metrics()
        handler = self.HandlerClass(_unlimited_config(), metrics=m, agent_id="lc-test")
        handler.on_llm_start({}, [])
        m.record_decision.assert_called_once_with("lc-test", "ALLOW")

    def test_halt_emits_record_decision(self) -> None:
        m = _make_metrics()
        handler = self.HandlerClass(_exhausted_config(), metrics=m, agent_id="lc-test")
        with pytest.raises(VeronicaHalt):
            handler.on_llm_start({}, [])
        m.record_decision.assert_called_once_with("lc-test", "HALT")

    def test_on_llm_end_emits_record_tokens(self) -> None:
        m = _make_metrics()
        handler = self.HandlerClass(_unlimited_config(), metrics=m, agent_id="lc-test")
        response = self.FakeLLMResult(
            llm_output={"token_usage": {"prompt_tokens": 120, "completion_tokens": 80}}
        )
        handler.on_llm_end(response)
        m.record_tokens.assert_called_once_with("lc-test", 120, 80)

    def test_on_llm_end_no_usage_no_emit(self) -> None:
        m = _make_metrics()
        handler = self.HandlerClass(_unlimited_config(), metrics=m, agent_id="lc-test")
        handler.on_llm_end(self.FakeLLMResult(llm_output={}))
        m.record_tokens.assert_not_called()

    def test_none_metrics_no_crash(self) -> None:
        handler = self.HandlerClass(_unlimited_config(), metrics=None)
        handler.on_llm_start({}, [])  # must not raise


# ---------------------------------------------------------------------------
# LangGraph adapter metrics tests
# ---------------------------------------------------------------------------


def _build_fake_langgraph_stubs() -> None:
    if "langgraph" not in sys.modules:
        lg = types.ModuleType("langgraph")
        sys.modules["langgraph"] = lg


class TestLangGraphAdapterMetrics:
    def setup_method(self) -> None:
        _build_fake_langchain_stubs()
        _build_fake_langgraph_stubs()
        if "veronica_core.adapters.langgraph" in sys.modules:
            del sys.modules["veronica_core.adapters.langgraph"]
        from veronica_core.adapters.langgraph import VeronicaLangGraphCallback

        self.CallbackClass = VeronicaLangGraphCallback

    def test_allow_emits_record_decision(self) -> None:
        m = _make_metrics()
        cb = self.CallbackClass(_unlimited_config(), metrics=m, agent_id="lg-test")
        cb.on_llm_start({}, [])
        m.record_decision.assert_called_once_with("lg-test", "ALLOW")

    def test_halt_emits_record_decision(self) -> None:
        m = _make_metrics()
        cb = self.CallbackClass(_exhausted_config(), metrics=m, agent_id="lg-test")
        with pytest.raises(VeronicaHalt):
            cb.on_llm_start({}, [])
        m.record_decision.assert_called_once_with("lg-test", "HALT")

    def test_node_wrapper_allow_emits(self) -> None:
        from veronica_core.adapters.langgraph import veronica_node_wrapper

        m = _make_metrics()

        @veronica_node_wrapper(_unlimited_config(), metrics=m, agent_id="node-test")
        def my_node(state: dict) -> dict:
            return state

        my_node({})
        m.record_decision.assert_called_once_with("node-test", "ALLOW")

    def test_node_wrapper_halt_emits(self) -> None:
        from veronica_core.adapters.langgraph import veronica_node_wrapper

        m = _make_metrics()

        @veronica_node_wrapper(_exhausted_config(), metrics=m, agent_id="node-test")
        def my_node(state: dict) -> dict:
            return state

        with pytest.raises(VeronicaHalt):
            my_node({})
        m.record_decision.assert_called_once_with("node-test", "HALT")

    def test_none_metrics_no_crash(self) -> None:
        cb = self.CallbackClass(_unlimited_config(), metrics=None)
        cb.on_llm_start({}, [])  # must not raise


# ---------------------------------------------------------------------------
# AG2 adapter metrics tests
# ---------------------------------------------------------------------------


def _build_fake_ag2_stubs() -> None:
    if "autogen" in sys.modules:
        return
    autogen = types.ModuleType("autogen")

    class FakeConversableAgent:
        def __init__(self, name: str, **kwargs: Any) -> None:
            self.name = name

        def generate_reply(self, messages=None, sender=None, **kwargs: Any) -> str:
            return "reply"

        def register_reply(
            self, trigger: Any, reply_func: Any, position: int = 0
        ) -> None:
            pass

    autogen.ConversableAgent = FakeConversableAgent
    sys.modules["autogen"] = autogen


class TestAG2AdapterMetrics:
    def setup_method(self) -> None:
        _build_fake_ag2_stubs()
        if "veronica_core.adapters.ag2" in sys.modules:
            del sys.modules["veronica_core.adapters.ag2"]
        from veronica_core.adapters.ag2 import VeronicaConversableAgent

        self.AgentClass = VeronicaConversableAgent

    def test_allow_emits_record_decision(self) -> None:
        m = _make_metrics()
        agent = self.AgentClass(
            "bot", _unlimited_config(), metrics=m, agent_id="ag2-test"
        )
        agent.generate_reply()
        m.record_decision.assert_called_once_with("ag2-test", "ALLOW")

    def test_halt_emits_record_decision(self) -> None:
        m = _make_metrics()
        agent = self.AgentClass(
            "bot", _exhausted_config(), metrics=m, agent_id="ag2-test"
        )
        with pytest.raises(VeronicaHalt):
            agent.generate_reply()
        m.record_decision.assert_called_once_with("ag2-test", "HALT")

    def test_default_agent_id_is_agent_name(self) -> None:
        m = _make_metrics()
        agent = self.AgentClass("my-agent", _unlimited_config(), metrics=m)
        agent.generate_reply()
        m.record_decision.assert_called_once_with("my-agent", "ALLOW")

    def test_none_metrics_no_crash(self) -> None:
        agent = self.AgentClass("bot", _unlimited_config(), metrics=None)
        agent.generate_reply()  # must not raise
