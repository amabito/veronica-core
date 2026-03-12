"""MetricsDrivenPolicy -- OTel metrics-based runtime policy for VERONICA.

Evaluates per-agent metrics (cost, tokens, latency, errors) against
declarative MetricRule thresholds and returns PolicyDecision.

Implements RuntimePolicy protocol: check(), reset(), policy_type property.

Zero external dependencies. Thread-safe via threading.Lock.
Dataclasses only for configuration.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from veronica_core.runtime_policy import (
    PolicyContext,
    PolicyDecision,
    deny,
)

logger = logging.getLogger(__name__)

_POLICY_TYPE = "metric_rule"

# ---------------------------------------------------------------------------
# Operator map
# ---------------------------------------------------------------------------

_OPERATORS: dict[str, str] = {
    "gt": ">",
    "lt": "<",
    "gte": ">=",
    "lte": "<=",
    "eq": "==",
}


def _evaluate_operator(value: float, op: str, threshold: float) -> bool:
    """Evaluate ``value <op> threshold``.

    Args:
        value:     Observed metric value.
        op:        Operator string ("gt", "lt", "gte", "lte", "eq").
        threshold: Comparison threshold.

    Returns:
        True if the condition holds (i.e. the rule is triggered).

    Raises:
        ValueError: if *op* is not a recognised operator.
    """
    if op == "gt":
        return value > threshold
    if op == "lt":
        return value < threshold
    if op == "gte":
        return value >= threshold
    if op == "lte":
        return value <= threshold
    if op == "eq":
        return value == threshold
    raise ValueError(f"Unknown operator {op!r}. Valid operators: {sorted(_OPERATORS)}")


# ---------------------------------------------------------------------------
# MetricRule
# ---------------------------------------------------------------------------


@dataclass
class MetricRule:
    """Declarative threshold rule for a single metric field.

    Attributes:
        metric:    Name of the metric to inspect on AgentMetrics.
                   Supported: "total_cost_usd", "total_tokens",
                   "avg_latency_ms", "error_count", "error_rate".
        operator:  Comparison operator.
                   One of: "gt", "lt", "gte", "lte", "eq".
        threshold: Numeric value to compare against.
        action:    Action to take when the condition holds.
                   One of: "halt", "degrade", "warn".
        agent_id:  Optional agent_id filter.  When set, only metrics for
                   this agent are inspected.  When None (default), the
                   rule checks against the global aggregate if available.
        label:     Optional human-readable label for diagnostics.
    """

    metric: str
    operator: str
    threshold: float
    action: str
    agent_id: Optional[str] = None
    label: str = ""

    _VALID_METRICS: frozenset[str] = field(
        default=frozenset(
            {
                "total_cost_usd",  # maps to AgentMetrics.total_cost
                "total_tokens",
                "avg_latency_ms",
                "error_count",
                "error_rate",
            }
        ),
        init=False,
        repr=False,
        compare=False,
    )
    _VALID_ACTIONS: frozenset[str] = field(
        default=frozenset({"halt", "degrade", "warn"}),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        errors: list[str] = []
        if self.metric not in self._VALID_METRICS:
            errors.append(
                f"MetricRule.metric={self.metric!r} is invalid; "
                f"valid: {sorted(self._VALID_METRICS)}"
            )
        if self.operator not in _OPERATORS:
            errors.append(
                f"MetricRule.operator={self.operator!r} is invalid; "
                f"valid: {sorted(_OPERATORS)}"
            )
        if self.action not in self._VALID_ACTIONS:
            errors.append(
                f"MetricRule.action={self.action!r} is invalid; "
                f"valid: {sorted(self._VALID_ACTIONS)}"
            )
        if not isinstance(self.threshold, (int, float)):
            errors.append(
                f"MetricRule.threshold must be numeric, got {type(self.threshold).__name__}"
            )
        elif not math.isfinite(float(self.threshold)):
            # NaN threshold silently disables the rule (NaN comparisons always return False).
            # ±inf thresholds cause always-trigger or never-trigger behaviour for some
            # operators (e.g. value < inf → always True → always halt).
            # Both are almost certainly configuration mistakes, so we reject them eagerly.
            errors.append(
                f"MetricRule.threshold must be finite, got {self.threshold!r}"
            )
        if errors:
            raise ValueError("; ".join(errors))

    def triggered(self, value: float) -> bool:
        """Return True if *value* satisfies the threshold condition."""
        return _evaluate_operator(value, self.operator, self.threshold)


# ---------------------------------------------------------------------------
# Module-level default OTelMetricsIngester (lazy)
# ---------------------------------------------------------------------------

_DEFAULT_INGESTER_LOCK = threading.Lock()
_default_ingester: Any = None  # OTelMetricsIngester | None


def set_default_ingester(ingester: Any) -> None:
    """Set the module-level default OTelMetricsIngester.

    Must be called before constructing MetricsDrivenPolicy instances that
    do not receive an explicit ingester.

    Args:
        ingester: An OTelMetricsIngester instance (or compatible object).
    """
    global _default_ingester
    with _DEFAULT_INGESTER_LOCK:
        _default_ingester = ingester


def get_default_ingester() -> Any:
    """Return the module-level default OTelMetricsIngester (may be None)."""
    with _DEFAULT_INGESTER_LOCK:
        return _default_ingester


# ---------------------------------------------------------------------------
# PolicyDecision builders
# ---------------------------------------------------------------------------


def _make_decision(action: str, policy_type: str, reason: str) -> PolicyDecision:
    """Map an action string to a PolicyDecision.

    Args:
        action:      One of "halt", "degrade", "warn".
        policy_type: Policy type string for the decision.
        reason:      Human-readable explanation.

    Returns:
        PolicyDecision appropriate for the action.
    """
    if action == "halt":
        return deny(policy_type=policy_type, reason=reason)
    if action == "degrade":
        return PolicyDecision(
            allowed=True,
            policy_type=policy_type,
            reason=reason,
            degradation_action="MODEL_DOWNGRADE",
        )
    # action == "warn"
    return PolicyDecision(
        allowed=True,
        policy_type=policy_type,
        reason=f"WARN: {reason}",
    )


# ---------------------------------------------------------------------------
# MetricsDrivenPolicy
# ---------------------------------------------------------------------------


class MetricsDrivenPolicy:
    """OTel metrics-driven runtime policy implementing RuntimePolicy.

    Fetches per-agent metrics from an OTelMetricsIngester, evaluates each
    MetricRule, and returns the first triggered decision (most severe first
    if multiple rules trigger: halt > degrade > warn).

    If the ingester returns no metrics for an agent, all rules are skipped
    (safe-by-default: allow).

    Thread-safe: rule list guarded by Lock.

    Example::

        from veronica_core.otel_feedback import OTelMetricsIngester
        from veronica_core.policy.metrics_policy import (
            MetricsDrivenPolicy, MetricRule, set_default_ingester
        )

        ingester = OTelMetricsIngester()
        set_default_ingester(ingester)

        policy = MetricsDrivenPolicy(
            rules=[
                MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot"),
                MetricRule("error_rate", "gt", 0.1, "degrade"),
            ]
        )
        decision = policy.check(PolicyContext(entity_id="bot"))
    """

    def __init__(
        self,
        rules: Optional[list[MetricRule]] = None,
        ingester: Any = None,
        agent_id: Optional[str] = None,
    ) -> None:
        """Create a MetricsDrivenPolicy.

        Args:
            rules:    List of MetricRule instances.  Evaluated in order;
                      first triggered rule produces the decision.
                      Defaults to empty list (always allow).
            ingester: OTelMetricsIngester instance.  Falls back to the
                      module-level default set via set_default_ingester().
            agent_id: Default agent_id to look up when a MetricRule does
                      not specify its own agent_id.  Also falls back to
                      PolicyContext.entity_id at check time.
        """
        self._rules: list[MetricRule] = list(rules or [])
        self._ingester: Any = ingester  # OTelMetricsIngester | None
        self._agent_id = agent_id
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # RuntimePolicy protocol
    # ------------------------------------------------------------------

    @property
    def policy_type(self) -> str:
        return _POLICY_TYPE

    def check(self, context: PolicyContext) -> PolicyDecision:
        """Evaluate all MetricRules against current ingester metrics.

        Resolution order for agent_id:
          1. MetricRule.agent_id (most specific)
          2. self._agent_id (policy-level default)
          3. context.entity_id

        If the ingester is unavailable (not set) or returns no metrics,
        all rules are skipped and an ALLOW decision is returned.

        Args:
            context: PolicyContext (entity_id used as fallback agent_id).

        Returns:
            PolicyDecision -- first triggered rule, or allow-all if none.
        """
        ingester = self._ingester or get_default_ingester()

        with self._lock:
            rules = list(self._rules)

        if not rules or ingester is None:
            return PolicyDecision(
                allowed=True,
                policy_type=_POLICY_TYPE,
                reason="No rules or ingester; skipping metric check",
            )

        default_agent = self._agent_id or context.entity_id or None

        # Severity ordering: halt > degrade > warn
        # Collect and return highest-severity triggered decision.
        _SEVERITY: dict[str, int] = {"halt": 3, "degrade": 2, "warn": 1}
        best: Optional[tuple[int, PolicyDecision]] = None

        for rule in rules:
            agent_id = rule.agent_id or default_agent
            metrics = self._get_metrics(ingester, agent_id)
            if metrics is None:
                continue

            value = self._extract_metric(metrics, rule.metric)
            if value is None:
                continue

            if rule.triggered(value):
                label = rule.label or f"{rule.metric} {rule.operator} {rule.threshold}"
                reason = (
                    f"Metric rule triggered: {label} "
                    f"(observed={value:.4g}, threshold={rule.threshold:.4g})"
                )
                if agent_id:
                    reason = f"[agent={agent_id}] {reason}"
                decision = _make_decision(rule.action, _POLICY_TYPE, reason)
                sev = _SEVERITY.get(rule.action, 0)
                if best is None or sev > best[0]:
                    best = (sev, decision)

        if best is not None:
            return best[1]

        return PolicyDecision(
            allowed=True,
            policy_type=_POLICY_TYPE,
            reason="All metric rules passed",
        )

    def reset(self) -> None:
        """Reset policy state.

        MetricsDrivenPolicy is stateless beyond its rule list; the ingester
        holds state.  This is a no-op but satisfies the RuntimePolicy protocol.
        """

    # ------------------------------------------------------------------
    # Rule management helpers
    # ------------------------------------------------------------------

    def add_rule(self, rule: MetricRule) -> None:
        """Append a MetricRule to the evaluation list.

        Thread-safe.

        Args:
            rule: MetricRule to add.
        """
        with self._lock:
            self._rules.append(rule)

    def clear_rules(self) -> None:
        """Remove all MetricRules.

        Thread-safe.
        """
        with self._lock:
            self._rules.clear()

    @property
    def rules(self) -> list[MetricRule]:
        """Snapshot of current rule list (copy).

        Thread-safe.
        """
        with self._lock:
            return list(self._rules)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_metrics(ingester: Any, agent_id: Optional[str]) -> Any:
        """Retrieve AgentMetrics from ingester for agent_id.

        Tries get_agent_metrics() first (OTelMetricsIngester API), then
        falls back to get_metrics() for compatibility with alternate backends.
        Returns None if unavailable (safe fallback).
        """
        if agent_id is None:
            return None
        try:
            if hasattr(ingester, "get_agent_metrics"):
                return ingester.get_agent_metrics(agent_id)
            return ingester.get_metrics(agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MetricsDrivenPolicy: ingester.get_agent_metrics(%r) failed: %s -- rules skipped for this agent",
                agent_id,
                exc,
            )
            return None

    # Alias mapping: rule metric name -> AgentMetrics attribute name
    _METRIC_ATTR_MAP: dict[str, str] = {
        "total_cost_usd": "total_cost",
        "total_tokens": "total_tokens",
        "avg_latency_ms": "avg_latency_ms",
        "error_count": "error_count",
        "error_rate": "error_rate",
    }

    @classmethod
    def _extract_metric(cls, metrics: Any, metric_name: str) -> Optional[float]:
        """Extract a named metric field from an AgentMetrics object.

        Applies _METRIC_ATTR_MAP to translate rule metric names to
        actual attribute names on AgentMetrics.
        Also supports dict-style access.
        Returns None if the field is missing or not a number.
        """
        # Resolve the actual attribute name
        attr_name = cls._METRIC_ATTR_MAP.get(metric_name, metric_name)
        try:
            if hasattr(metrics, attr_name):
                value = getattr(metrics, attr_name)
            elif hasattr(metrics, f"_{attr_name}"):
                # Fallback: duck-typed objects may expose only the private field
                value = getattr(metrics, f"_{attr_name}")
            elif isinstance(metrics, dict):
                # Try both the mapped name and the original name
                value = metrics.get(attr_name)
                if value is None and attr_name != metric_name:
                    value = metrics.get(metric_name)
            else:
                return None

            if value is None:
                return None
            result = float(value)
            if not math.isfinite(result):
                return None
            return result
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "MetricsDrivenPolicy: cannot extract metric %r (attr=%r): %s",
                metric_name,
                attr_name,
                exc,
            )
            return None
