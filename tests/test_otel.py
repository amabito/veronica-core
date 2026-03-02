"""Tests for OpenTelemetry integration (P2-1)."""
from __future__ import annotations

import pytest

from veronica_core.otel import (
    OTelExecutionGraphObserver,
    disable_otel,
    emit_containment_decision,
    emit_safety_event,
    enable_otel_with_provider,
    enable_otel_with_tracer,
    get_tracer,
    is_otel_enabled,
)
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision


@pytest.fixture(autouse=True)
def reset_otel():
    disable_otel()
    yield
    disable_otel()


def _setup_test_otel():
    """Create an in-memory OTel tracer for testing."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    enable_otel_with_tracer(tracer)
    return exporter, tracer


def _make_event(reason: str = "test reason") -> SafetyEvent:
    return SafetyEvent(
        event_type="BUDGET_EXCEEDED",
        decision=Decision.HALT,
        reason=reason,
        hook="BudgetBoundaryHook",
        request_id="req-001",
    )


# ---------------------------------------------------------------------------
# Basic enable/disable
# ---------------------------------------------------------------------------


def test_otel_disabled_by_default():
    assert is_otel_enabled() is False


def test_enable_with_tracer():
    class FakeTracer:
        pass

    enable_otel_with_tracer(FakeTracer())
    assert is_otel_enabled() is True


def test_disable_otel():
    enable_otel_with_tracer(object())
    disable_otel()
    assert is_otel_enabled() is False


# ---------------------------------------------------------------------------
# No-op when disabled
# ---------------------------------------------------------------------------


def test_emit_safety_event_noop_when_disabled():
    event = _make_event()
    # Must not raise
    emit_safety_event(event)


def test_emit_containment_decision_noop_when_disabled():
    # Must not raise
    emit_containment_decision("HALT", "budget exceeded", cost_usd=1.23)


# ---------------------------------------------------------------------------
# Functional tests with InMemorySpanExporter
# ---------------------------------------------------------------------------


def test_emit_safety_event_with_tracer():
    exporter, tracer = _setup_test_otel()

    with tracer.start_as_current_span("test-span"):
        emit_safety_event(_make_event())

    finished = exporter.get_finished_spans()
    assert finished, "Expected at least one finished span"
    span = finished[0]
    event_names = [e.name for e in span.events]
    assert any(name.startswith("veronica.") for name in event_names), (
        f"Expected veronica.* event, got: {event_names}"
    )


def test_emit_no_prompt_content():
    """Verify that no 'prompt' or 'content' keys are exported."""
    exporter, tracer = _setup_test_otel()

    with tracer.start_as_current_span("privacy-span"):
        emit_safety_event(_make_event("sensitive reason"))

    finished = exporter.get_finished_spans()
    span = finished[0]
    for otel_event in span.events:
        for key in otel_event.attributes or {}:
            assert "prompt" not in key.lower(), f"Unexpected 'prompt' key: {key}"
            assert "content" not in key.lower(), f"Unexpected 'content' key: {key}"


def test_reason_truncated():
    """Reason longer than 500 chars should be truncated to 500 in the attribute."""
    exporter, tracer = _setup_test_otel()
    long_reason = "x" * 600

    with tracer.start_as_current_span("truncate-span"):
        emit_safety_event(_make_event(reason=long_reason))

    finished = exporter.get_finished_spans()
    span = finished[0]
    otel_event = span.events[0]
    reason_val = otel_event.attributes.get("veronica.reason", "")
    assert len(reason_val) <= 500


def test_emit_containment_decision_with_tracer():
    exporter, tracer = _setup_test_otel()

    with tracer.start_as_current_span("containment-span"):
        emit_containment_decision("HALT", "budget limit reached", cost_usd=9.99, chain_id="chain-1")

    finished = exporter.get_finished_spans()
    span = finished[0]
    event_names = [e.name for e in span.events]
    assert any("containment" in name for name in event_names)


# ---------------------------------------------------------------------------
# Pipeline integration: OTel emitted on non-ALLOW decision
# ---------------------------------------------------------------------------


def test_pipeline_emits_otel_on_halt():
    """ShieldPipeline._record should emit OTel event when OTel is enabled."""

    from veronica_core.shield.hooks import PreDispatchHook
    from veronica_core.shield.pipeline import ShieldPipeline
    from veronica_core.shield.types import Decision, ToolCallContext

    exporter, tracer = _setup_test_otel()

    class HaltHook(PreDispatchHook):
        def before_llm_call(self, ctx):
            return Decision.HALT

    pipeline = ShieldPipeline(pre_dispatch=HaltHook())
    ctx = ToolCallContext(request_id="req-otel-test")

    with tracer.start_as_current_span("pipeline-span"):
        result = pipeline.before_llm_call(ctx)

    assert result == Decision.HALT
    finished = exporter.get_finished_spans()
    span = finished[0]
    # At least one veronica.* event should have been added
    event_names = [e.name for e in span.events]
    assert any(name.startswith("veronica.") for name in event_names), (
        f"Expected veronica.* OTel event, got: {event_names}"
    )


# ---------------------------------------------------------------------------
# enable_otel_with_provider
# ---------------------------------------------------------------------------


class TestEnableOTelWithProvider:
    """Tests for sharing an external TracerProvider."""

    def test_provider_enables_otel(self):
        """enable_otel_with_provider activates OTel using external provider."""
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider()
        enable_otel_with_provider(provider)
        assert is_otel_enabled() is True

    def test_provider_tracer_emits_events(self):
        """Events emitted through a shared provider appear on the trace."""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        enable_otel_with_provider(provider)
        tracer = provider.get_tracer("test-caller")

        with tracer.start_as_current_span("ag2-agent-span"):
            emit_containment_decision("ALLOW", "all checks passed")

        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        event_names = [e.name for e in finished[0].events]
        assert any("veronica" in n for n in event_names)

    def test_provider_bad_object_does_not_crash(self):
        """Non-provider object should not crash, just log warning."""
        enable_otel_with_provider("not a provider")
        assert is_otel_enabled() is False


# ---------------------------------------------------------------------------
# get_tracer
# ---------------------------------------------------------------------------


class TestGetTracer:
    """Tests for get_tracer() helper."""

    def test_returns_none_when_disabled(self):
        assert get_tracer() is None

    def test_returns_tracer_when_enabled(self):
        enable_otel_with_tracer(object())
        assert get_tracer() is not None


# ---------------------------------------------------------------------------
# OTelExecutionGraphObserver
# ---------------------------------------------------------------------------


class TestOTelExecutionGraphObserver:
    """Tests for OTelExecutionGraphObserver."""

    def _make_otel_env(self):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        enable_otel_with_provider(provider)
        tracer = provider.get_tracer("test")
        return exporter, tracer

    def test_on_node_start_emits_event(self):
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()

        with tracer.start_as_current_span("agent-span"):
            observer.on_node_start("n000001", "plan_step", {"key": "val"})

        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        assert any(e.name == "veronica.node.start" for e in events)

    def test_on_node_complete_emits_event(self):
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()

        with tracer.start_as_current_span("agent-span"):
            observer.on_node_complete("n000001", cost_usd=0.05, duration_ms=123.4)

        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        complete_events = [e for e in events if e.name == "veronica.node.complete"]
        assert len(complete_events) == 1
        attrs = complete_events[0].attributes
        assert attrs["veronica.cost_usd"] == 0.05
        assert attrs["veronica.duration_ms"] == 123.4

    def test_on_node_failed_emits_event(self):
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()

        with tracer.start_as_current_span("agent-span"):
            observer.on_node_failed("n000001", "TimeoutError")

        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        failed_events = [e for e in events if e.name == "veronica.node.failed"]
        assert len(failed_events) == 1
        assert failed_events[0].attributes["veronica.error"] == "TimeoutError"

    def test_on_decision_emits_event(self):
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()

        with tracer.start_as_current_span("agent-span"):
            observer.on_decision("n000001", "HALT", "budget exceeded")

        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        decision_events = [e for e in events if e.name == "veronica.node.decision"]
        assert len(decision_events) == 1
        attrs = decision_events[0].attributes
        assert attrs["veronica.decision"] == "HALT"
        assert attrs["veronica.reason"] == "budget exceeded"

    def test_noop_when_otel_disabled(self):
        """All observer methods are no-ops when OTel is not enabled."""
        disable_otel()
        observer = OTelExecutionGraphObserver()
        # Must not raise
        observer.on_node_start("n1", "op", {})
        observer.on_node_complete("n1", 0.0, 0.0)
        observer.on_node_failed("n1", "err")
        observer.on_decision("n1", "ALLOW", "ok")

    def test_reason_truncated_at_500(self):
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()
        long_reason = "x" * 600

        with tracer.start_as_current_span("agent-span"):
            observer.on_decision("n1", "HALT", long_reason)

        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        decision_events = [e for e in events if e.name == "veronica.node.decision"]
        assert len(decision_events[0].attributes["veronica.reason"]) <= 500

    def test_observer_satisfies_protocol(self):
        """OTelExecutionGraphObserver must satisfy ExecutionGraphObserver protocol."""
        from veronica_core.protocols import ExecutionGraphObserver as Protocol
        observer = OTelExecutionGraphObserver()
        assert isinstance(observer, Protocol)

    def test_wired_to_execution_graph(self):
        """Observer receives callbacks when wired to ExecutionGraph."""
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()

        from veronica_core.containment.execution_graph import ExecutionGraph

        graph = ExecutionGraph(observers=[observer])

        with tracer.start_as_current_span("graph-span"):
            node_id = graph.create_root("test_op")
            graph.mark_running(node_id)
            graph.mark_success(node_id, cost_usd=0.01)

        spans = exporter.get_finished_spans()
        event_names = [e.name for s in spans for e in s.events]
        assert "veronica.node.start" in event_names
        assert "veronica.node.complete" in event_names


# ---------------------------------------------------------------------------
# AG2 Capability OTel integration
# ---------------------------------------------------------------------------


class TestAG2CapabilityOTel:
    """OTel events emitted by CircuitBreakerCapability."""

    def _make_otel_env(self):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        enable_otel_with_provider(provider)
        tracer = provider.get_tracer("test")
        return exporter, tracer

    def _make_stub(self, name="agent-1", reply="hello"):
        class StubAgent:
            def __init__(self):
                self.name = name

            def generate_reply(self, *args, **kwargs):
                return reply

        return StubAgent()

    def test_allow_event_on_success(self):
        """ALLOW event emitted when all checks pass."""
        exporter, tracer = self._make_otel_env()
        from veronica_core.adapters.ag2_capability import CircuitBreakerCapability

        agent = self._make_stub()
        cap = CircuitBreakerCapability(failure_threshold=3)
        cap.add_to_agent(agent)

        with tracer.start_as_current_span("agent-span"):
            result = agent.generate_reply()

        assert result == "hello"
        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        event_names = [e.name for e in events]
        assert any("allow" in n.lower() for n in event_names)

    def test_halt_event_on_circuit_open(self):
        """HALT event emitted when circuit breaker is OPEN."""
        exporter, tracer = self._make_otel_env()
        from veronica_core.adapters.ag2_capability import CircuitBreakerCapability

        agent = self._make_stub(reply=None)
        cap = CircuitBreakerCapability(failure_threshold=2, recovery_timeout=9999)
        cap.add_to_agent(agent)

        # Trip the breaker
        agent.generate_reply()
        agent.generate_reply()

        exporter.clear()

        with tracer.start_as_current_span("blocked-span"):
            result = agent.generate_reply()

        assert result is None
        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        halt_events = [e for e in events if "halt" in e.name.lower()]
        assert len(halt_events) >= 1

    def test_no_event_when_otel_disabled(self):
        """No crash when OTel is disabled and capability runs."""
        disable_otel()
        from veronica_core.adapters.ag2_capability import CircuitBreakerCapability

        agent = self._make_stub()
        cap = CircuitBreakerCapability(failure_threshold=3)
        cap.add_to_agent(agent)
        result = agent.generate_reply()
        assert result == "hello"
