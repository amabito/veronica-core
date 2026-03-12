"""OTel Feedback -- Ingest OpenTelemetry spans and derive per-agent metrics.

Provides:
- AgentMetrics: dataclass tracking per-agent cost/token/latency/error stats
- OTelMetricsIngester: thread-safe span parser that builds AgentMetrics per agent_id
- MetricRule: declarative threshold rule for a single metric field
- MetricsDrivenPolicy: OTel metrics-driven runtime policy implementing RuntimePolicy

Span formats supported:
- AG2 native spans (span_type: conversation, agent, llm, tool, code_execution)
- Generic OTel spans with llm.token.count, llm.cost attributes
- veronica-core spans with veronica.cost_usd, veronica.decision attributes

Zero external dependencies. Thread-safe via threading.Lock.
"""

from veronica_core.otel_feedback.ingester import AgentMetrics, OTelMetricsIngester
from veronica_core.policy.metrics_policy import MetricRule, MetricsDrivenPolicy

__all__ = ["AgentMetrics", "OTelMetricsIngester", "MetricRule", "MetricsDrivenPolicy"]
