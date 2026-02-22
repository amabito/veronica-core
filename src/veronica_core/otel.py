"""OpenTelemetry integration for VERONICA containment events.

Privacy: prompt/response content is NEVER exported. Only structured
metadata (event_type, decision, reason snippet, cost_usd) is emitted.

Usage:
    from veronica_core.otel import enable_otel
    enable_otel(service_name="my-agent")
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from veronica_core.shield.event import SafetyEvent

logger = logging.getLogger(__name__)

_otel_enabled: bool = False
_tracer: Any = None


def enable_otel(
    service_name: str,
    exporter: Any = None,
    endpoint: str | None = None,
) -> None:
    """Enable OTel export. Requires opentelemetry-sdk installed separately."""
    global _otel_enabled, _tracer
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider

        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)

        if exporter is not None:
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor

            provider.add_span_processor(SimpleSpanProcessor(exporter))
        elif endpoint:
            os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("veronica-core")
        _otel_enabled = True
        logger.info("VERONICA OTel enabled: service=%r", service_name)
    except ImportError as exc:
        logger.warning("opentelemetry-sdk not installed. OTel disabled. %s", exc)


def enable_otel_with_tracer(tracer: Any) -> None:
    """Enable OTel using an existing tracer (for testing)."""
    global _otel_enabled, _tracer
    _tracer = tracer
    _otel_enabled = True


def is_otel_enabled() -> bool:
    """Return True if OTel is currently enabled."""
    return _otel_enabled


def disable_otel() -> None:
    """Disable OTel and clear the tracer."""
    global _otel_enabled, _tracer
    _otel_enabled = False
    _tracer = None


def emit_safety_event(event: "SafetyEvent") -> None:
    """Emit SafetyEvent as OTel span event. No-op if OTel not enabled."""
    if not _otel_enabled or _tracer is None:
        return
    try:
        from opentelemetry import trace

        current_span = trace.get_current_span()
        if current_span is not None:
            current_span.add_event(
                name=f"veronica.{event.event_type.lower()}",
                attributes={
                    "veronica.event_type": str(event.event_type),
                    "veronica.decision": str(
                        event.decision.value
                        if hasattr(event.decision, "value")
                        else event.decision
                    ),
                    "veronica.hook": str(event.hook or ""),
                    "veronica.reason": (event.reason or "")[:500],
                    "veronica.request_id": str(event.request_id or ""),
                },
            )
    except Exception as exc:
        logger.debug("OTel emit_safety_event failed: %s", exc)


def emit_containment_decision(
    decision_name: str,
    reason: str,
    cost_usd: float = 0.0,
    chain_id: str = "",
) -> None:
    """Emit containment decision as OTel event."""
    if not _otel_enabled or _tracer is None:
        return
    try:
        from opentelemetry import trace

        current_span = trace.get_current_span()
        if current_span is not None:
            current_span.add_event(
                name=f"veronica.containment.{decision_name.lower()}",
                attributes={
                    "veronica.decision": decision_name,
                    "veronica.reason": reason[:500],
                    "veronica.cost_usd": cost_usd,
                    "veronica.chain_id": chain_id,
                },
            )
    except Exception as exc:
        logger.debug("OTel emit_containment_decision failed: %s", exc)
