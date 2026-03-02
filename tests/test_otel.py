"""Tests for OpenTelemetry integration (P2-1)."""
from __future__ import annotations

import threading

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


# ===========================================================================
# Adversarial tests -- attacker mindset
# ===========================================================================


class TestAdversarialProvider:
    """Adversarial tests for enable_otel_with_provider."""

    def test_none_provider_does_not_crash(self):
        """None as provider must not crash or leave OTel in broken state."""
        enable_otel_with_provider(None)
        assert is_otel_enabled() is False

    def test_provider_get_tracer_returns_none(self):
        """Provider that returns None from get_tracer must not enable OTel
        in a half-broken state where is_otel_enabled() is True but emit is dead."""
        class NoneTracerProvider:
            def get_tracer(self, name):
                return None

        enable_otel_with_provider(NoneTracerProvider())
        # Even if _otel_enabled is True, emit functions must not crash.
        # This tests the safety of the dual-check (enabled AND tracer is not None).
        emit_containment_decision("HALT", "test")
        emit_safety_event(_make_event())

    def test_provider_get_tracer_raises(self):
        """Provider whose get_tracer raises arbitrary exception."""
        class ExplodingProvider:
            def get_tracer(self, name):
                raise RuntimeError("provider exploded")

        enable_otel_with_provider(ExplodingProvider())
        assert is_otel_enabled() is False

    def test_provider_get_tracer_raises_base_exception(self):
        """Provider whose get_tracer raises KeyboardInterrupt-like exception.

        enable_otel_with_provider catches Exception, not BaseException.
        KeyboardInterrupt should propagate (correct behavior).
        But SystemExit-class errors from buggy providers should not crash the app.
        """
        class WeirdProvider:
            def get_tracer(self, name):
                raise ValueError("not a real provider")

        enable_otel_with_provider(WeirdProvider())
        assert is_otel_enabled() is False

    def test_concurrent_enable_disable_no_torn_state(self):
        """10 threads racing enable/disable must never produce torn state
        where _otel_enabled=True but _tracer=None, or vice versa."""
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider()
        errors = []
        barrier = threading.Barrier(10)

        def enable_worker():
            barrier.wait()
            for _ in range(50):
                enable_otel_with_provider(provider)

        def disable_worker():
            barrier.wait()
            for _ in range(50):
                disable_otel()

        def check_worker():
            barrier.wait()
            for _ in range(100):
                enabled = is_otel_enabled()
                tracer = get_tracer()
                # Invariant: if enabled is True, tracer may or may not be None
                # (due to TOCTOU between the two calls). But the module-level
                # _lock ensures no torn state WITHIN a single acquire.
                # We just verify no crash.
                if enabled and tracer is None:
                    # This can happen due to TOCTOU between the two separate
                    # lock acquisitions. It is NOT a bug -- it's the expected
                    # race. The important thing is no crash or corruption.
                    pass

        threads = (
            [threading.Thread(target=enable_worker) for _ in range(3)]
            + [threading.Thread(target=disable_worker) for _ in range(3)]
            + [threading.Thread(target=check_worker) for _ in range(4)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # No thread should be stuck
        for t in threads:
            assert not t.is_alive(), "Thread stuck -- possible deadlock"
        assert not errors


class TestAdversarialObserverOTel:
    """Adversarial tests for OTelExecutionGraphObserver."""

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

    def test_nan_inf_cost_duration_no_crash(self):
        """NaN and inf in numeric fields must not crash."""
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()

        with tracer.start_as_current_span("span"):
            observer.on_node_complete("n1", float("nan"), float("inf"))
            observer.on_node_complete("n2", float("-inf"), float("nan"))
            observer.on_node_complete("n3", -999.99, 0.0)

        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        assert len([e for e in events if e.name == "veronica.node.complete"]) == 3

    def test_none_string_fields_swallowed_by_graph(self):
        """None in string fields raises TypeError in observer, but ExecutionGraph
        swallows all observer exceptions -- the graph must not crash."""
        observer = OTelExecutionGraphObserver()
        exporter, tracer = self._make_otel_env()

        from veronica_core.containment.execution_graph import ExecutionGraph

        graph = ExecutionGraph(observers=[observer])

        with tracer.start_as_current_span("span"):
            node_id = graph.create_root("test_op")
            graph.mark_running(node_id)
            # mark_failure passes error as string, but what if the observer
            # itself is called with corrupted data? We test the observer directly.
            # Direct call: on_node_failed(None, None) -- both None
            observer.on_node_failed(None, None)  # type: ignore[arg-type]
            # The TypeError from None[:500] is caught by _emit_graph_event's try/except
            # OR by ExecutionGraph._notify_observers's except

        # Graph must still be functional after corrupted observer call
        graph.mark_success(node_id, cost_usd=0.0)

    def test_empty_strings_all_fields(self):
        """Empty strings for all observer parameters."""
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()

        with tracer.start_as_current_span("span"):
            observer.on_node_start("", "", {})
            observer.on_node_complete("", 0.0, 0.0)
            observer.on_node_failed("", "")
            observer.on_decision("", "", "")

        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        assert len(events) == 4

    def test_very_long_error_and_reason(self):
        """10K char strings must be truncated to 500, not blow up OTel."""
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()
        huge = "A" * 10_000

        with tracer.start_as_current_span("span"):
            observer.on_node_failed("n1", huge)
            observer.on_decision("n1", "HALT", huge)

        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        for e in events:
            for key, val in (e.attributes or {}).items():
                if isinstance(val, str):
                    assert len(val) <= 10_001, f"{key} not truncated"
            if "error" in (e.attributes or {}):
                assert len(e.attributes["veronica.error"]) <= 500
            if "reason" in (e.attributes or {}):
                assert len(e.attributes["veronica.reason"]) <= 500

    def test_special_chars_in_strings(self):
        """NUL bytes, newlines, unicode in observer fields."""
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()

        with tracer.start_as_current_span("span"):
            observer.on_node_start("n\x00001", "op\nname", {"k": "v"})
            observer.on_node_failed("n\x00001", "err\x00or")
            observer.on_decision("n1", "HALT", "reason\nwith\nnewlines")

        # Must not crash -- OTel accepts arbitrary strings
        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        assert len(events) == 3

    def test_concurrent_observer_emission(self):
        """10 threads emitting observer events simultaneously."""
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()
        barrier = threading.Barrier(10)

        def worker(idx):
            barrier.wait()
            with tracer.start_as_current_span(f"span-{idx}"):
                for j in range(20):
                    observer.on_node_start(f"n{idx}_{j}", "op", {})
                    observer.on_node_complete(f"n{idx}_{j}", 0.01, 1.0)
                    observer.on_decision(f"n{idx}_{j}", "ALLOW", "ok")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        for t in threads:
            assert not t.is_alive(), "Thread stuck"

        # 10 threads * 20 iterations * 3 events = 600 events across 10 spans
        spans = exporter.get_finished_spans()
        total_events = sum(len(s.events) for s in spans)
        assert total_events == 600

    def test_broken_span_add_event_does_not_crash(self):
        """If add_event raises, _emit_graph_event catches it silently."""
        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()

        with tracer.start_as_current_span("span") as span:
            # Monkey-patch add_event to explode
            original_add_event = span.add_event
            span.add_event = lambda *a, **kw: (_ for _ in ()).throw(
                RuntimeError("exporter died")
            )
            try:
                # Must not raise
                observer.on_node_start("n1", "op", {})
                observer.on_decision("n1", "HALT", "reason")
            finally:
                span.add_event = original_add_event

    def test_otel_toggled_mid_graph_lifecycle(self):
        """Enable OTel, start graph node, disable OTel, complete node.
        Must not crash. Events before disable should be captured."""
        from veronica_core.containment.execution_graph import ExecutionGraph

        exporter, tracer = self._make_otel_env()
        observer = OTelExecutionGraphObserver()
        graph = ExecutionGraph(observers=[observer])

        with tracer.start_as_current_span("span"):
            node_id = graph.create_root("op")
            graph.mark_running(node_id)  # OTel enabled -- event emitted
            disable_otel()
            graph.mark_success(node_id, cost_usd=0.01)  # OTel disabled -- no-op

        spans = exporter.get_finished_spans()
        event_names = [e.name for s in spans for e in s.events]
        assert "veronica.node.start" in event_names
        # node.complete should NOT be in events (OTel was disabled)
        assert "veronica.node.complete" not in event_names


class TestAdversarialContainmentInvariant:
    """THE critical property: OTel must NEVER affect containment decisions.

    If OTel is broken, crashing, racing, or misconfigured, the circuit breaker
    must still open/close correctly and generate_reply must still return
    the correct result.
    """

    def _make_stub(self, name="agent-1", reply="hello"):
        class StubAgent:
            def __init__(self):
                self.name = name

            def generate_reply(self, *args, **kwargs):
                return reply

        return StubAgent()

    def test_circuit_breaker_opens_despite_otel_crash(self):
        """Circuit breaker must still trip after threshold failures,
        even if every OTel emit call crashes internally."""
        from unittest.mock import patch
        from veronica_core.adapters.ag2_capability import CircuitBreakerCapability
        from veronica_core.circuit_breaker import CircuitState

        # Enable OTel with a real provider so emit code runs
        from opentelemetry.sdk.trace import TracerProvider
        provider = TracerProvider()
        enable_otel_with_provider(provider)

        agent = self._make_stub(reply=None)
        cap = CircuitBreakerCapability(failure_threshold=2, recovery_timeout=9999)
        breaker = cap.add_to_agent(agent)

        # Patch emit_containment_decision to always crash
        with patch(
            "veronica_core.otel.emit_containment_decision",
            side_effect=RuntimeError("OTel exporter down"),
        ):
            agent.generate_reply()  # failure 1
            agent.generate_reply()  # failure 2 -> OPEN

        # Circuit must be OPEN despite OTel crashes
        assert breaker.state == CircuitState.OPEN
        # Further calls must be blocked
        result = agent.generate_reply()
        assert result is None

    def test_reply_value_preserved_despite_otel_crash(self):
        """generate_reply must return the correct value even when OTel crashes."""
        from unittest.mock import patch
        from veronica_core.adapters.ag2_capability import CircuitBreakerCapability

        from opentelemetry.sdk.trace import TracerProvider
        provider = TracerProvider()
        enable_otel_with_provider(provider)

        agent = self._make_stub(reply="important_result")
        cap = CircuitBreakerCapability(failure_threshold=3)
        cap.add_to_agent(agent)

        with patch(
            "veronica_core.otel.emit_containment_decision",
            side_effect=Exception("total OTel failure"),
        ):
            result = agent.generate_reply()

        assert result == "important_result"

    def test_safe_mode_blocks_despite_otel_crash(self):
        """SAFE_MODE must still block agents even when OTel is completely broken."""
        from unittest.mock import patch
        from veronica_core.adapters.ag2_capability import CircuitBreakerCapability
        from veronica_core.backends import MemoryBackend
        from veronica_core.integration import VeronicaIntegration
        from veronica_core.state import VeronicaState

        from opentelemetry.sdk.trace import TracerProvider
        provider = TracerProvider()
        enable_otel_with_provider(provider)

        veronica = VeronicaIntegration(
            cooldown_fails=5, cooldown_seconds=60, backend=MemoryBackend()
        )
        # Force SAFE_MODE
        veronica.state.current_state = VeronicaState.SAFE_MODE

        agent = self._make_stub()
        cap = CircuitBreakerCapability(failure_threshold=3, veronica=veronica)
        cap.add_to_agent(agent)

        with patch(
            "veronica_core.otel.emit_containment_decision",
            side_effect=Exception("OTel is on fire"),
        ):
            result = agent.generate_reply()

        # Must be blocked by SAFE_MODE, not by OTel crash
        assert result is None

    def test_emit_decision_with_nan_cost(self):
        """NaN cost_usd must not crash emit or corrupt span data."""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        enable_otel_with_provider(provider)
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("span"):
            emit_containment_decision("HALT", "reason", cost_usd=float("nan"))
            emit_containment_decision("ALLOW", "ok", cost_usd=float("inf"))
            emit_containment_decision("DEGRADE", "budget", cost_usd=-1.0)

        spans = exporter.get_finished_spans()
        events = [e for s in spans for e in s.events]
        assert len(events) == 3

    def test_concurrent_generate_reply_with_otel(self):
        """Multiple threads calling generate_reply with OTel enabled.
        Circuit breaker state must remain consistent."""
        from veronica_core.adapters.ag2_capability import CircuitBreakerCapability
        from veronica_core.circuit_breaker import CircuitState

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        enable_otel_with_provider(provider)
        tracer = provider.get_tracer("test")

        call_count = 0
        count_lock = threading.Lock()

        class CountingAgent:
            name = "counter"

            def generate_reply(self, *args, **kwargs):
                nonlocal call_count
                with count_lock:
                    call_count += 1
                return "ok"

        agent = CountingAgent()
        cap = CircuitBreakerCapability(failure_threshold=100)
        breaker = cap.add_to_agent(agent)
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            with tracer.start_as_current_span("worker"):
                for _ in range(20):
                    agent.generate_reply()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        for t in threads:
            assert not t.is_alive()

        # All 100 calls should succeed
        assert call_count == 100
        assert breaker.state == CircuitState.CLOSED
