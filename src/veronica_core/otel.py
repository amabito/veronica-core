"""OpenTelemetry integration for VERONICA containment events.

Privacy: prompt/response content is NEVER exported. Only structured
metadata (event_type, decision, reason snippet, cost_usd) is emitted.

Usage::

    from veronica_core.otel import enable_otel
    enable_otel(service_name="my-agent")

    # Share an external TracerProvider (e.g. AG2's):
    from veronica_core.otel import enable_otel_with_provider
    enable_otel_with_provider(ag2_tracer_provider)

    # Attach observer to ExecutionGraph:
    from veronica_core.otel import OTelExecutionGraphObserver
    graph = ExecutionGraph(observers=[OTelExecutionGraphObserver()])
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from veronica_core.shield.event import SafetyEvent

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# Set once during enable_otel(); read-only after. No lock needed for reads.
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
        new_tracer = trace.get_tracer("veronica-core")
        with _lock:
            _tracer = new_tracer
            _otel_enabled = True
        logger.info("VERONICA OTel enabled: service=%r", service_name)
    except ImportError as exc:
        logger.warning("opentelemetry-sdk not installed. OTel disabled. %s", exc)


def enable_otel_with_provider(tracer_provider: Any) -> None:
    """Enable OTel using an external TracerProvider.

    Use this when another system (e.g. AG2's OpenTelemetry tracing) already
    manages the TracerProvider. veronica-core creates its own tracer from the
    shared provider so containment events appear in the same trace tree.

    Args:
        tracer_provider: An ``opentelemetry.sdk.trace.TracerProvider`` (or
            compatible) instance.  veronica-core calls
            ``tracer_provider.get_tracer("veronica-core")`` to obtain its
            tracer.

    Example::

        # AG2 + veronica-core sharing a single TracerProvider:
        from opentelemetry.sdk.trace import TracerProvider
        provider = TracerProvider()
        ag2.runtime_logging.start(logger_type="otel", tracer_provider=provider)
        enable_otel_with_provider(provider)
    """
    global _otel_enabled, _tracer
    try:
        new_tracer = tracer_provider.get_tracer("veronica-core")
        with _lock:
            _tracer = new_tracer
            _otel_enabled = True
        logger.info("VERONICA OTel enabled via external TracerProvider")
    except Exception as exc:
        logger.warning("Failed to enable OTel with external provider: %s", exc)


def enable_otel_with_tracer(tracer: Any) -> None:
    """Enable OTel using an existing tracer (for testing)."""
    global _otel_enabled, _tracer
    with _lock:
        _tracer = tracer
        _otel_enabled = True


def is_otel_enabled() -> bool:
    """Return True if OTel is currently enabled."""
    with _lock:
        return _otel_enabled


def disable_otel() -> None:
    """Disable OTel and clear the tracer."""
    global _otel_enabled, _tracer
    with _lock:
        _otel_enabled = False
        _tracer = None


def emit_safety_event(event: "SafetyEvent") -> None:
    """Emit SafetyEvent as OTel span event. No-op if OTel not enabled."""
    with _lock:
        enabled = _otel_enabled
        tracer = _tracer
    if not enabled or tracer is None:
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
    with _lock:
        enabled = _otel_enabled
        tracer = _tracer
    if not enabled or tracer is None:
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


def get_tracer() -> Any:
    """Return the veronica-core OTel tracer, or None if not enabled.

    Useful for AG2 adapters that need to create child spans.
    """
    with _lock:
        return _tracer if _otel_enabled else None


# ---------------------------------------------------------------------------
# OTelExecutionGraphObserver
# ---------------------------------------------------------------------------


class OTelExecutionGraphObserver:
    """ExecutionGraphObserver that emits lifecycle events to OpenTelemetry.

    Attach to an ExecutionGraph to get containment lifecycle events in your
    OTel trace. Each observer callback adds an event to the current active
    span (typically an AG2 agent or conversation span).

    This class does NOT create new spans -- it annotates existing spans
    created by the caller or by framework adapters (AG2, LangChain, etc.).

    Requires OTel to be enabled via ``enable_otel()``,
    ``enable_otel_with_provider()``, or ``enable_otel_with_tracer()`` first.
    All methods are no-ops if OTel is not enabled.

    Example::

        from veronica_core.otel import OTelExecutionGraphObserver, enable_otel
        from veronica_core.containment.execution_graph import ExecutionGraph

        enable_otel(service_name="my-agent")
        observer = OTelExecutionGraphObserver()
        graph = ExecutionGraph(observers=[observer])
    """

    def on_node_start(self, node_id: str, operation: str, metadata: dict) -> None:
        """Emit a ``veronica.node.start`` event on the current span."""
        _emit_graph_event(
            "veronica.node.start",
            {
                "veronica.node_id": node_id,
                "veronica.operation": operation,
            },
        )

    def on_node_complete(
        self, node_id: str, cost_usd: float, duration_ms: float
    ) -> None:
        """Emit a ``veronica.node.complete`` event on the current span."""
        _emit_graph_event(
            "veronica.node.complete",
            {
                "veronica.node_id": node_id,
                "veronica.cost_usd": cost_usd,
                "veronica.duration_ms": duration_ms,
            },
        )

    def on_node_failed(self, node_id: str, error: str) -> None:
        """Emit a ``veronica.node.failed`` event on the current span."""
        _emit_graph_event(
            "veronica.node.failed",
            {
                "veronica.node_id": str(node_id or ""),
                "veronica.error": str(error or "")[:500],
            },
        )

    def on_decision(self, node_id: str, decision: str, reason: str) -> None:
        """Emit a ``veronica.node.decision`` event on the current span."""
        _emit_graph_event(
            "veronica.node.decision",
            {
                "veronica.node_id": str(node_id or ""),
                "veronica.decision": str(decision or ""),
                "veronica.reason": str(reason or "")[:500],
            },
        )


def _emit_graph_event(name: str, attributes: dict[str, Any]) -> None:
    """Internal helper: add an event to the current OTel span."""
    with _lock:
        enabled = _otel_enabled
    if not enabled:
        return
    try:
        from opentelemetry import trace

        current_span = trace.get_current_span()
        if current_span is not None:
            current_span.add_event(name=name, attributes=attributes)
    except Exception as exc:
        logger.debug("OTel _emit_graph_event failed: %s", exc)
