"""LoggingContainmentMetrics -- reference implementation of ContainmentMetricsProtocol.

Forwards all containment telemetry to Python's logging module using structured
log records. Useful as a default when no external metrics backend is available,
and as a reference when implementing a custom ContainmentMetricsProtocol.

Usage::

    from veronica_core.metrics.logging_metrics import LoggingContainmentMetrics

    metrics = LoggingContainmentMetrics()

    # Pass to ExecutionContext
    from veronica_core.containment import ExecutionContext, ExecutionConfig
    ctx = ExecutionContext(config=config, metrics=metrics)

    # Or use directly
    metrics.record_cost("planner", 0.0042)
    metrics.record_tokens("planner", 120, 80)
    metrics.record_decision("planner", "ALLOW")
    metrics.record_circuit_state("llm-service", "OPEN")
    metrics.record_latency("planner", 312.5)
"""

from __future__ import annotations

import logging
from typing import Optional


__all__ = ["LoggingContainmentMetrics"]

logger = logging.getLogger(__name__)


class LoggingContainmentMetrics:
    """ContainmentMetricsProtocol implementation that logs via Python logging.

    Each method emits a structured log record at DEBUG level. The logger name
    is ``veronica_core.metrics.logging_metrics`` so that callers can configure
    verbosity independently from other veronica-core loggers.

    Args:
        log_level: Python logging level for all emitted records.
            Defaults to ``logging.DEBUG``.
        logger_name: Override the logger name. Useful when running multiple
            instances in the same process with different destinations.
    """

    def __init__(
        self,
        log_level: int = logging.DEBUG,
        logger_name: Optional[str] = None,
    ) -> None:
        self._log_level = log_level
        self._logger = logging.getLogger(logger_name or __name__)

    def record_cost(self, agent_id: str, cost_usd: float) -> None:
        """Log USD cost for one LLM call."""
        self._logger.log(
            self._log_level,
            "[VERONICA_METRICS] cost agent_id=%s cost_usd=%.6f",
            agent_id,
            cost_usd,
        )

    def record_tokens(
        self, agent_id: str, input_tokens: int, output_tokens: int
    ) -> None:
        """Log token counts for one LLM call."""
        self._logger.log(
            self._log_level,
            "[VERONICA_METRICS] tokens agent_id=%s input=%d output=%d",
            agent_id,
            input_tokens,
            output_tokens,
        )

    def record_decision(self, agent_id: str, decision: str) -> None:
        """Log a containment decision (ALLOW, HALT, DEGRADE, COOLDOWN)."""
        self._logger.log(
            self._log_level,
            "[VERONICA_METRICS] decision agent_id=%s decision=%s",
            agent_id,
            decision,
        )

    def record_circuit_state(self, entity_id: str, state: str) -> None:
        """Log the current circuit breaker state."""
        self._logger.log(
            self._log_level,
            "[VERONICA_METRICS] circuit entity_id=%s state=%s",
            entity_id,
            state,
        )

    def record_latency(self, agent_id: str, duration_ms: float) -> None:
        """Log wall-clock latency for one call."""
        self._logger.log(
            self._log_level,
            "[VERONICA_METRICS] latency agent_id=%s duration_ms=%.2f",
            agent_id,
            duration_ms,
        )
