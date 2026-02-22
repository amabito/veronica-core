"""Tests for OpenTelemetry integration (P2-1)."""
from __future__ import annotations

import pytest

from veronica_core.otel import (
    disable_otel,
    emit_containment_decision,
    emit_safety_event,
    enable_otel,
    enable_otel_with_tracer,
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
    from opentelemetry import trace
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
    from unittest.mock import MagicMock

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
