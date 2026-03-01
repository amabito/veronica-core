"""Tests for veronica_core.adapters.llamaindex.

LlamaIndex is an optional dependency. These tests use mocks to avoid
requiring llama-index-core to be installed.
"""
from __future__ import annotations

import sys
import types

import pytest

from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.inject import GuardConfig, VeronicaHalt


# ---------------------------------------------------------------------------
# Helpers: build minimal llama_index stub
# ---------------------------------------------------------------------------


def _make_llama_stub() -> types.ModuleType:
    """Build a minimal llama_index.core stub sufficient for the adapter."""

    class _CBEventType:
        LLM = "llm"
        QUERY = "query"
        RETRIEVE = "retrieve"

    class _BaseCallbackHandler:
        def __init__(
            self,
            event_starts_to_ignore: list | None = None,
            event_ends_to_ignore: list | None = None,
        ) -> None:
            self.event_starts_to_ignore = event_starts_to_ignore or []
            self.event_ends_to_ignore = event_ends_to_ignore or []

        def on_event_start(self, event_type, payload=None, event_id="", **kwargs):
            return event_id

        def on_event_end(self, event_type, payload=None, event_id="", **kwargs):
            pass

        def start_trace(self, trace_id=None):
            pass

        def end_trace(self, trace_id=None, trace_map=None):
            pass

    # Build module hierarchy
    root = types.ModuleType("llama_index")
    core = types.ModuleType("llama_index.core")
    callbacks = types.ModuleType("llama_index.core.callbacks")
    schema = types.ModuleType("llama_index.core.callbacks.schema")

    callbacks.BaseCallbackHandler = _BaseCallbackHandler
    schema.CBEventType = _CBEventType
    core.callbacks = callbacks

    sys.modules["llama_index"] = root
    sys.modules["llama_index.core"] = core
    sys.modules["llama_index.core.callbacks"] = callbacks
    sys.modules["llama_index.core.callbacks.schema"] = schema

    return root


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def inject_llama_stub(monkeypatch):
    """Inject llama_index stub before each test and clean up after."""
    # Remove any previously cached adapter module
    for key in list(sys.modules.keys()):
        if "llamaindex" in key and "veronica" in key:
            del sys.modules[key]
    # Also remove cached llama_index modules
    for key in list(sys.modules.keys()):
        if key.startswith("llama_index"):
            del sys.modules[key]

    _make_llama_stub()

    yield

    # Cleanup
    for key in list(sys.modules.keys()):
        if key.startswith("llama_index"):
            del sys.modules[key]
        if "llamaindex" in key and "veronica" in key:
            del sys.modules[key]


def _make_handler(**kwargs) -> "VeronicaLlamaIndexHandler":  # noqa: F821
    from veronica_core.adapters.llamaindex import VeronicaLlamaIndexHandler
    return VeronicaLlamaIndexHandler(**kwargs)


# ---------------------------------------------------------------------------
# Basic instantiation
# ---------------------------------------------------------------------------


class TestInstantiation:
    def test_creates_with_guard_config(self):
        config = GuardConfig(max_cost_usd=1.0, max_steps=20)
        handler = _make_handler(config=config)
        assert handler.container is not None
        assert handler.circuit_breaker is None

    def test_creates_with_circuit_breaker(self):
        config = GuardConfig(max_cost_usd=1.0, max_steps=20)
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
        handler = _make_handler(config=config, circuit_breaker=cb)
        assert handler.circuit_breaker is cb

    def test_import_error_without_llama_index(self):
        # Remove stub modules to simulate missing package
        for key in list(sys.modules.keys()):
            if key.startswith("llama_index"):
                del sys.modules[key]
        # Remove adapter cache
        for key in list(sys.modules.keys()):
            if "llamaindex" in key and "veronica" in key:
                del sys.modules[key]

        # Now import adapter (llama_index unavailable) — should raise at instantiation
        from veronica_core.adapters.llamaindex import VeronicaLlamaIndexHandler

        with pytest.raises(ImportError, match="llama-index-core"):
            VeronicaLlamaIndexHandler(GuardConfig(max_cost_usd=1.0))


# ---------------------------------------------------------------------------
# on_event_start — LLM events
# ---------------------------------------------------------------------------


class TestOnEventStart:
    def _cbtype(self):
        from llama_index.core.callbacks.schema import CBEventType
        return CBEventType

    def test_allows_llm_event_within_budget(self):
        config = GuardConfig(max_cost_usd=10.0, max_steps=100)
        handler = _make_handler(config=config)
        CBEventType = self._cbtype()

        event_id = handler.on_event_start(CBEventType.LLM)
        assert isinstance(event_id, str)

    def test_passes_through_non_llm_events(self):
        config = GuardConfig(max_cost_usd=0.0, max_steps=0)
        handler = _make_handler(config=config)
        CBEventType = self._cbtype()

        # QUERY events should not trigger policy check even when budget=0
        event_id = handler.on_event_start(CBEventType.QUERY)
        assert isinstance(event_id, str)

    def test_raises_veronica_halt_when_steps_exhausted(self):
        config = GuardConfig(max_cost_usd=10.0, max_steps=2)
        handler = _make_handler(config=config)
        CBEventType = self._cbtype()

        # Exhaust steps via on_event_end
        for _ in range(2):
            handler.on_event_start(CBEventType.LLM)
            handler.on_event_end(CBEventType.LLM)

        with pytest.raises(VeronicaHalt):
            handler.on_event_start(CBEventType.LLM)

    def test_raises_veronica_halt_when_budget_exceeded(self):
        config = GuardConfig(max_cost_usd=0.001, max_steps=100)
        handler = _make_handler(config=config)
        CBEventType = self._cbtype()

        # Drain budget manually
        handler.container.budget.spend(0.002)

        with pytest.raises(VeronicaHalt):
            handler.on_event_start(CBEventType.LLM)

    def test_returns_provided_event_id(self):
        config = GuardConfig(max_cost_usd=10.0, max_steps=100)
        handler = _make_handler(config=config)
        CBEventType = self._cbtype()

        returned = handler.on_event_start(CBEventType.LLM, event_id="my-id-123")
        assert returned == "my-id-123"

    def test_generates_event_id_when_empty(self):
        config = GuardConfig(max_cost_usd=10.0, max_steps=100)
        handler = _make_handler(config=config)
        CBEventType = self._cbtype()

        returned = handler.on_event_start(CBEventType.LLM, event_id="")
        assert len(returned) > 0


# ---------------------------------------------------------------------------
# on_event_end — step counting and cost recording
# ---------------------------------------------------------------------------


class TestOnEventEnd:
    def _cbtype(self):
        from llama_index.core.callbacks.schema import CBEventType
        return CBEventType

    def test_increments_step_count(self):
        config = GuardConfig(max_cost_usd=10.0, max_steps=100)
        handler = _make_handler(config=config)
        CBEventType = self._cbtype()

        handler.on_event_start(CBEventType.LLM)
        handler.on_event_end(CBEventType.LLM)
        assert handler.container.step_guard.current_step == 1

    def test_records_cost_from_usage_payload(self):
        config = GuardConfig(max_cost_usd=10.0, max_steps=100)
        handler = _make_handler(config=config)
        CBEventType = self._cbtype()

        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
            }
        }
        handler.on_event_start(CBEventType.LLM)
        handler.on_event_end(CBEventType.LLM, payload=payload)

        # Cost should be recorded (non-zero)
        assert handler.container.budget.spent_usd > 0.0

    def test_non_llm_events_do_not_increment_steps(self):
        config = GuardConfig(max_cost_usd=10.0, max_steps=100)
        handler = _make_handler(config=config)
        CBEventType = self._cbtype()

        handler.on_event_end(CBEventType.QUERY)
        assert handler.container.step_guard.current_step == 0

    def test_no_cost_recorded_for_missing_payload(self):
        config = GuardConfig(max_cost_usd=10.0, max_steps=100)
        handler = _make_handler(config=config)
        CBEventType = self._cbtype()

        handler.on_event_start(CBEventType.LLM)
        handler.on_event_end(CBEventType.LLM, payload=None)
        assert handler.container.budget.spent_usd == 0.0


# ---------------------------------------------------------------------------
# Circuit breaker integration
# ---------------------------------------------------------------------------


class TestCircuitBreakerIntegration:
    def _cbtype(self):
        from llama_index.core.callbacks.schema import CBEventType
        return CBEventType

    def test_circuit_open_raises_veronica_halt(self):
        config = GuardConfig(max_cost_usd=10.0, max_steps=100)
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=3600.0)
        handler = _make_handler(config=config, circuit_breaker=cb, entity_id="test-cb")
        CBEventType = self._cbtype()

        # Open the circuit
        cb.record_failure()

        with pytest.raises(VeronicaHalt):
            handler.on_event_start(CBEventType.LLM)

    def test_successful_call_records_success(self):
        config = GuardConfig(max_cost_usd=10.0, max_steps=100)
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=3600.0)
        handler = _make_handler(config=config, circuit_breaker=cb)
        CBEventType = self._cbtype()

        handler.on_event_start(CBEventType.LLM)
        handler.on_event_end(CBEventType.LLM)

        assert cb.failure_count == 0  # reset on success

    def test_llm_error_records_failure(self):
        config = GuardConfig(max_cost_usd=10.0, max_steps=100)
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=3600.0)
        handler = _make_handler(config=config, circuit_breaker=cb)

        handler.on_llm_error(RuntimeError("test error"))
        assert cb.failure_count == 1


# ---------------------------------------------------------------------------
# _extract_cost_from_payload unit tests
# ---------------------------------------------------------------------------


class TestExtractCostFromPayload:
    def _extract(self, payload):
        from veronica_core.adapters.llamaindex import _extract_cost_from_payload
        return _extract_cost_from_payload(payload)

    def test_returns_zero_for_empty_dict(self):
        assert self._extract({}) == 0.0

    def test_extracts_from_usage_dict(self):
        payload = {"usage": {"prompt_tokens": 1000, "completion_tokens": 500}}
        cost = self._extract(payload)
        assert cost > 0.0

    def test_extracts_anthropic_format(self):
        payload = {"usage": {"input_tokens": 1000, "output_tokens": 500}}
        cost = self._extract(payload)
        assert cost > 0.0

    def test_returns_zero_on_malformed_usage(self):
        payload = {"usage": "not-a-dict"}
        assert self._extract(payload) == 0.0

    def test_no_exception_on_unexpected_structure(self):
        payload = {"response": object()}
        # Should not raise
        cost = self._extract(payload)
        assert isinstance(cost, float)
