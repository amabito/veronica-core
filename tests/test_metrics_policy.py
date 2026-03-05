"""Unit tests for MetricsDrivenPolicy, MetricRule, and PolicyRegistry integration.

Covers:
- MetricRule: validation, operator evaluation, action mapping
- MetricsDrivenPolicy: check() logic, severity ordering, thread safety
- PolicyRegistry: metric_rule factory registration
- Edge cases: no ingester, missing metrics, unknown agent_id
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

import pytest

from veronica_core.policy.metrics_policy import (
    MetricRule,
    MetricsDrivenPolicy,
    _evaluate_operator,
    _make_decision,
    get_default_ingester,
    set_default_ingester,
)
from veronica_core.runtime_policy import PolicyContext, PolicyDecision


# ---------------------------------------------------------------------------
# Helpers / Stubs
# ---------------------------------------------------------------------------


@dataclass
class FakeMetrics:
    """Fake AgentMetrics-compatible object for testing."""

    total_cost: float = 0.0
    total_tokens: int = 0
    avg_latency_ms: float = 0.0
    error_rate: float = 0.0
    _error_count: int = 0
    call_count: int = 0


class FakeIngester:
    """Minimal OTelMetricsIngester stub."""

    def __init__(self, agents: Optional[dict[str, FakeMetrics]] = None) -> None:
        self._agents = agents or {}

    def get_agent_metrics(self, agent_id: str) -> FakeMetrics:
        return self._agents.get(agent_id, FakeMetrics())

    def set_agent(self, agent_id: str, metrics: FakeMetrics) -> None:
        self._agents[agent_id] = metrics


def _ctx(entity_id: str = "") -> PolicyContext:
    return PolicyContext(entity_id=entity_id)


# ---------------------------------------------------------------------------
# _evaluate_operator tests
# ---------------------------------------------------------------------------


class TestEvaluateOperator:
    def test_gt_true(self) -> None:
        assert _evaluate_operator(5.0, "gt", 3.0) is True

    def test_gt_false(self) -> None:
        assert _evaluate_operator(2.0, "gt", 3.0) is False

    def test_lt_true(self) -> None:
        assert _evaluate_operator(1.0, "lt", 3.0) is True

    def test_lt_false(self) -> None:
        assert _evaluate_operator(5.0, "lt", 3.0) is False

    def test_gte_equal(self) -> None:
        assert _evaluate_operator(3.0, "gte", 3.0) is True

    def test_lte_equal(self) -> None:
        assert _evaluate_operator(3.0, "lte", 3.0) is True

    def test_eq(self) -> None:
        assert _evaluate_operator(2.5, "eq", 2.5) is True

    def test_invalid_operator_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown operator"):
            _evaluate_operator(1.0, "invalid", 0.0)


# ---------------------------------------------------------------------------
# MetricRule validation tests
# ---------------------------------------------------------------------------


class TestMetricRuleValidation:
    def test_valid_rule_creates(self) -> None:
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt")
        assert rule.metric == "total_cost_usd"
        assert rule.operator == "gt"
        assert rule.threshold == 1.0
        assert rule.action == "halt"

    def test_invalid_metric_raises(self) -> None:
        with pytest.raises(ValueError, match="MetricRule.metric"):
            MetricRule("unknown_metric", "gt", 1.0, "halt")

    def test_invalid_operator_raises(self) -> None:
        with pytest.raises(ValueError, match="MetricRule.operator"):
            MetricRule("total_cost_usd", "ne", 1.0, "halt")

    def test_invalid_action_raises(self) -> None:
        with pytest.raises(ValueError, match="MetricRule.action"):
            MetricRule("total_cost_usd", "gt", 1.0, "queue")

    def test_non_numeric_threshold_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            MetricRule("total_cost_usd", "gt", "bad", "halt")  # type: ignore[arg-type]

    def test_nan_threshold_raises(self) -> None:
        """NaN threshold silently disables all comparisons — must be rejected."""
        import math

        with pytest.raises(ValueError, match="finite"):
            MetricRule("total_cost_usd", "gt", math.nan, "halt")

    def test_pos_inf_threshold_raises(self) -> None:
        """+inf threshold with 'lt' causes always-trigger DoS — must be rejected."""
        import math

        with pytest.raises(ValueError, match="finite"):
            MetricRule("error_rate", "lt", math.inf, "halt")

    def test_neg_inf_threshold_raises(self) -> None:
        """-inf threshold with 'gt' causes always-trigger DoS — must be rejected."""
        import math

        with pytest.raises(ValueError, match="finite"):
            MetricRule("total_tokens", "gt", -math.inf, "warn")

    def test_triggered_true(self) -> None:
        rule = MetricRule("total_cost_usd", "gt", 0.5, "halt")
        assert rule.triggered(1.0) is True

    def test_triggered_false(self) -> None:
        rule = MetricRule("total_cost_usd", "gt", 0.5, "halt")
        assert rule.triggered(0.3) is False

    def test_optional_agent_id(self) -> None:
        rule = MetricRule("error_rate", "gt", 0.1, "warn", agent_id="bot-1")
        assert rule.agent_id == "bot-1"

    def test_optional_label(self) -> None:
        rule = MetricRule("error_rate", "gt", 0.1, "warn", label="high error rate")
        assert rule.label == "high error rate"


# ---------------------------------------------------------------------------
# _make_decision tests
# ---------------------------------------------------------------------------


class TestMakeDecision:
    def test_halt_denied(self) -> None:
        d = _make_decision("halt", "metric_rule", "over budget")
        assert d.allowed is False
        assert d.policy_type == "metric_rule"

    def test_degrade_allowed_with_model_downgrade(self) -> None:
        d = _make_decision("degrade", "metric_rule", "high cost")
        assert d.allowed is True
        assert d.degradation_action == "MODEL_DOWNGRADE"

    def test_warn_allowed_with_warn_prefix(self) -> None:
        d = _make_decision("warn", "metric_rule", "approaching limit")
        assert d.allowed is True
        assert "WARN:" in d.reason


# ---------------------------------------------------------------------------
# MetricsDrivenPolicy — core logic tests
# ---------------------------------------------------------------------------


class TestMetricsDrivenPolicyBasic:
    def _policy_with_agent(
        self,
        agent_id: str,
        metrics: FakeMetrics,
        rules: list[MetricRule],
    ) -> MetricsDrivenPolicy:
        ingester = FakeIngester({agent_id: metrics})
        return MetricsDrivenPolicy(rules=rules, ingester=ingester, agent_id=agent_id)

    def test_allow_when_no_rules(self) -> None:
        ingester = FakeIngester()
        policy = MetricsDrivenPolicy(rules=[], ingester=ingester)
        d = policy.check(_ctx())
        assert d.allowed is True

    def test_allow_when_no_ingester(self) -> None:
        rule = MetricRule("total_cost_usd", "gt", 0.0, "halt")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=None)
        d = policy.check(_ctx())
        assert d.allowed is True

    def test_halt_triggered(self) -> None:
        m = FakeMetrics(total_cost=2.0)
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot")
        policy = self._policy_with_agent("bot", m, [rule])
        d = policy.check(_ctx())
        assert d.allowed is False
        assert "total_cost_usd" in d.reason or "Metric rule triggered" in d.reason

    def test_degrade_triggered(self) -> None:
        m = FakeMetrics(error_rate=0.2)
        rule = MetricRule("error_rate", "gt", 0.1, "degrade", agent_id="bot")
        policy = self._policy_with_agent("bot", m, [rule])
        d = policy.check(_ctx())
        assert d.allowed is True
        assert d.degradation_action == "MODEL_DOWNGRADE"

    def test_warn_triggered(self) -> None:
        m = FakeMetrics(total_tokens=1000)
        rule = MetricRule("total_tokens", "gte", 500, "warn", agent_id="bot")
        policy = self._policy_with_agent("bot", m, [rule])
        d = policy.check(_ctx())
        assert d.allowed is True
        assert "WARN:" in d.reason

    def test_not_triggered_when_below_threshold(self) -> None:
        m = FakeMetrics(total_cost=0.5)
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot")
        policy = self._policy_with_agent("bot", m, [rule])
        d = policy.check(_ctx())
        assert d.allowed is True

    def test_entity_id_fallback(self) -> None:
        m = FakeMetrics(total_cost=2.0)
        ingester = FakeIngester({"user-42": m})
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ingester)
        d = policy.check(_ctx(entity_id="user-42"))
        assert d.allowed is False

    def test_agent_id_on_rule_overrides_policy(self) -> None:
        m_a = FakeMetrics(total_cost=2.0)
        m_b = FakeMetrics(total_cost=0.1)
        ingester = FakeIngester({"agent-a": m_a, "agent-b": m_b})
        # Rule targets agent-b (below threshold) but policy default is agent-a
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="agent-b")
        policy = MetricsDrivenPolicy(
            rules=[rule], ingester=ingester, agent_id="agent-a"
        )
        d = policy.check(_ctx())
        assert d.allowed is True

    def test_unknown_agent_returns_allow(self) -> None:
        ingester = FakeIngester()  # no agents
        rule = MetricRule("total_cost_usd", "gt", 0.0, "halt", agent_id="ghost")
        # OTelMetricsIngester returns zeroed metrics for unknown agents
        # FakeIngester returns FakeMetrics() with total_cost=0.0 → not > 0.0
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ingester)
        d = policy.check(_ctx())
        assert d.allowed is True  # 0.0 is not > 0.0


# ---------------------------------------------------------------------------
# Severity ordering tests (halt > degrade > warn)
# ---------------------------------------------------------------------------


class TestSeverityOrdering:
    def test_halt_wins_over_degrade(self) -> None:
        m = FakeMetrics(total_cost=2.0, error_rate=0.2)
        ingester = FakeIngester({"bot": m})
        rules = [
            MetricRule("error_rate", "gt", 0.1, "degrade", agent_id="bot"),
            MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot"),
        ]
        policy = MetricsDrivenPolicy(rules=rules, ingester=ingester)
        d = policy.check(_ctx())
        assert d.allowed is False  # halt wins

    def test_halt_wins_over_warn(self) -> None:
        m = FakeMetrics(total_cost=2.0, avg_latency_ms=500.0)
        ingester = FakeIngester({"bot": m})
        rules = [
            MetricRule("avg_latency_ms", "gt", 100.0, "warn", agent_id="bot"),
            MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot"),
        ]
        policy = MetricsDrivenPolicy(rules=rules, ingester=ingester)
        d = policy.check(_ctx())
        assert d.allowed is False

    def test_degrade_wins_over_warn(self) -> None:
        m = FakeMetrics(error_rate=0.2, avg_latency_ms=200.0)
        ingester = FakeIngester({"bot": m})
        rules = [
            MetricRule("avg_latency_ms", "gt", 100.0, "warn", agent_id="bot"),
            MetricRule("error_rate", "gt", 0.1, "degrade", agent_id="bot"),
        ]
        policy = MetricsDrivenPolicy(rules=rules, ingester=ingester)
        d = policy.check(_ctx())
        assert d.allowed is True
        assert d.degradation_action == "MODEL_DOWNGRADE"


# ---------------------------------------------------------------------------
# Metric attribute mapping tests
# ---------------------------------------------------------------------------


class TestMetricAttributeMapping:
    def _policy(
        self, metric: str, op: str, threshold: float, action: str, m: FakeMetrics
    ) -> PolicyDecision:
        ingester = FakeIngester({"bot": m})
        rule = MetricRule(metric, op, threshold, action, agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ingester)
        return policy.check(_ctx())

    def test_total_cost_usd_maps_to_total_cost(self) -> None:
        m = FakeMetrics(total_cost=5.0)
        d = self._policy("total_cost_usd", "gt", 1.0, "halt", m)
        assert d.allowed is False

    def test_total_tokens(self) -> None:
        m = FakeMetrics(total_tokens=1000)
        d = self._policy("total_tokens", "gte", 1000, "warn", m)
        assert d.allowed is True
        assert "WARN:" in d.reason

    def test_avg_latency_ms(self) -> None:
        m = FakeMetrics(avg_latency_ms=300.0)
        d = self._policy("avg_latency_ms", "gt", 200.0, "degrade", m)
        assert d.allowed is True
        assert d.degradation_action == "MODEL_DOWNGRADE"

    def test_error_rate(self) -> None:
        m = FakeMetrics(error_rate=0.5)
        d = self._policy("error_rate", "gt", 0.3, "halt", m)
        assert d.allowed is False

    def test_error_count(self) -> None:
        m = FakeMetrics(_error_count=5)
        d = self._policy("error_count", "gte", 3, "warn", m)
        assert d.allowed is True
        assert "WARN:" in d.reason


# ---------------------------------------------------------------------------
# Rule management tests
# ---------------------------------------------------------------------------


class TestRuleManagement:
    def test_add_rule_after_creation(self) -> None:
        policy = MetricsDrivenPolicy(rules=[], ingester=None)
        assert len(policy.rules) == 0
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt")
        policy.add_rule(rule)
        assert len(policy.rules) == 1

    def test_clear_rules(self) -> None:
        rules = [MetricRule("error_rate", "gt", 0.1, "warn")]
        policy = MetricsDrivenPolicy(rules=rules, ingester=None)
        policy.clear_rules()
        assert len(policy.rules) == 0

    def test_rules_returns_copy(self) -> None:
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=None)
        snapshot = policy.rules
        snapshot.clear()  # modify copy
        assert len(policy.rules) == 1  # original unchanged

    def test_reset_is_noop(self) -> None:
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=None)
        policy.reset()  # must not raise
        assert len(policy.rules) == 1

    def test_policy_type(self) -> None:
        policy = MetricsDrivenPolicy()
        assert policy.policy_type == "metric_rule"


# ---------------------------------------------------------------------------
# Thread safety test
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_check_no_crash(self) -> None:
        m = FakeMetrics(total_cost=2.0)
        ingester = FakeIngester({"bot": m})
        rules = [MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot")]
        policy = MetricsDrivenPolicy(rules=rules, ingester=ingester)

        results: list[bool] = []
        errors: list[Exception] = []

        def _check() -> None:
            try:
                d = policy.check(_ctx(entity_id="bot"))
                results.append(d.allowed)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_check) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Exceptions in threads: {errors}"
        # All should be denied (cost=2.0 > 1.0)
        assert all(r is False for r in results)

    def test_concurrent_add_rule(self) -> None:
        policy = MetricsDrivenPolicy(rules=[], ingester=None)
        errors: list[Exception] = []

        def _add() -> None:
            try:
                policy.add_rule(MetricRule("error_rate", "gt", 0.1, "warn"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_add) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(policy.rules) == 10


# ---------------------------------------------------------------------------
# set_default_ingester / get_default_ingester tests
# ---------------------------------------------------------------------------


class TestDefaultIngester:
    def setup_method(self) -> None:
        # Reset default ingester before each test
        set_default_ingester(None)

    def teardown_method(self) -> None:
        set_default_ingester(None)

    def test_default_ingester_initially_none(self) -> None:
        assert get_default_ingester() is None

    def test_set_and_get_default_ingester(self) -> None:
        ingester = FakeIngester()
        set_default_ingester(ingester)
        assert get_default_ingester() is ingester

    def test_policy_uses_default_ingester(self) -> None:
        m = FakeMetrics(total_cost=5.0)
        ingester = FakeIngester({"agent-x": m})
        set_default_ingester(ingester)

        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="agent-x")
        # No explicit ingester — uses module default
        policy = MetricsDrivenPolicy(rules=[rule])
        d = policy.check(_ctx())
        assert d.allowed is False

    def test_explicit_ingester_overrides_default(self) -> None:
        m_default = FakeMetrics(total_cost=5.0)
        m_explicit = FakeMetrics(total_cost=0.1)
        default_ingester = FakeIngester({"bot": m_default})
        explicit_ingester = FakeIngester({"bot": m_explicit})
        set_default_ingester(default_ingester)

        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=explicit_ingester)
        d = policy.check(_ctx())
        assert d.allowed is True  # uses explicit (0.1 not > 1.0)


# ---------------------------------------------------------------------------
# PolicyRegistry integration
# ---------------------------------------------------------------------------


class TestPolicyRegistryMetricRule:
    def test_metric_rule_registered(self) -> None:
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        assert "metric_rule" in registry.known_types()

    def test_metric_rule_factory_creates_policy(self) -> None:
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        params = {
            "rules": [
                {
                    "metric": "total_cost_usd",
                    "operator": "gt",
                    "threshold": 1.0,
                    "action": "halt",
                    "agent_id": "bot",
                }
            ]
        }
        policy = factory(params)
        assert policy.policy_type == "metric_rule"
        assert len(policy.rules) == 1

    def test_metric_rule_factory_empty_rules(self) -> None:
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        policy = factory({})
        assert policy.policy_type == "metric_rule"
        assert len(policy.rules) == 0

    def test_metric_rule_factory_null_threshold_uses_default(self) -> None:
        """YAML/JSON null threshold (Python None) must not raise TypeError.

        When a config dict contains ``threshold: null`` (YAML) or
        ``"threshold": null`` (JSON), ``r.get("threshold", 0.0)`` returns
        ``None`` (the key exists), not ``0.0``. Calling ``float(None)`` then
        raises ``TypeError``. The factory must guard against this and fall
        back to ``0.0``.
        """
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        # threshold explicitly set to None simulates ``threshold: null`` in YAML
        params = {
            "rules": [
                {
                    "metric": "total_cost_usd",
                    "operator": "gt",
                    "threshold": None,
                    "action": "halt",
                }
            ]
        }
        policy = factory(params)
        assert policy.policy_type == "metric_rule"
        assert len(policy.rules) == 1
        assert policy.rules[0].threshold == 0.0

    def test_metric_rule_factory_null_metric_raises(self) -> None:
        """YAML/JSON null metric must raise TypeError, not silently default."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        params = {
            "rules": [
                {
                    "metric": None,
                    "operator": "gt",
                    "threshold": 1.0,
                    "action": "warn",
                }
            ]
        }
        with pytest.raises(TypeError, match="metric.*non-empty string"):
            factory(params)

    def test_metric_rule_factory_null_label_uses_empty_string(self) -> None:
        """YAML/JSON null label must not produce the string 'None'."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        params = {
            "rules": [
                {
                    "metric": "error_rate",
                    "operator": "gt",
                    "threshold": 0.5,
                    "action": "warn",
                    "label": None,
                }
            ]
        }
        policy = factory(params)
        assert len(policy.rules) == 1
        assert policy.rules[0].label == ""


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_no_agent_id_anywhere_skips_rules(self) -> None:
        m = FakeMetrics(total_cost=5.0)
        ingester = FakeIngester({"bot": m})
        # Rule has no agent_id, policy has no agent_id, context has no entity_id
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ingester)
        d = policy.check(_ctx(entity_id=""))
        # agent_id is None → get_metrics returns None → rule skipped → allow
        assert d.allowed is True

    def test_ingester_raises_exception_is_safe(self) -> None:
        class BrokenIngester:
            def get_agent_metrics(self, agent_id: str) -> None:
                raise RuntimeError("connection lost")

        rule = MetricRule("total_cost_usd", "gt", 0.0, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=BrokenIngester())
        d = policy.check(_ctx(entity_id="bot"))
        # Exception swallowed → safe allow
        assert d.allowed is True

    def test_multiple_rules_none_triggered(self) -> None:
        m = FakeMetrics(total_cost=0.5, error_rate=0.05, avg_latency_ms=50.0)
        ingester = FakeIngester({"bot": m})
        rules = [
            MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot"),
            MetricRule("error_rate", "gt", 0.1, "degrade", agent_id="bot"),
            MetricRule("avg_latency_ms", "gt", 100.0, "warn", agent_id="bot"),
        ]
        policy = MetricsDrivenPolicy(rules=rules, ingester=ingester)
        d = policy.check(_ctx())
        assert d.allowed is True

    def test_dict_based_metrics(self) -> None:
        """MetricsDrivenPolicy works with dict metrics (non-dataclass)."""

        class DictIngester:
            def get_agent_metrics(self, agent_id: str) -> dict:
                return {"total_cost": 3.0, "error_rate": 0.0, "total_tokens": 500}

        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=DictIngester())
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is False

    def test_agent_id_in_reason(self) -> None:
        m = FakeMetrics(total_cost=5.0)
        ingester = FakeIngester({"my-agent": m})
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="my-agent")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ingester)
        d = policy.check(_ctx())
        assert "my-agent" in d.reason


# ---------------------------------------------------------------------------
# YAML round-trip tests (metric_rule via PolicyLoader)
# ---------------------------------------------------------------------------


class TestYAMLRoundTrip:
    """Verify metric_rule can be loaded from YAML/JSON via PolicyLoader."""

    def test_metric_rule_yaml_load(self) -> None:
        from veronica_core.policy.loader import PolicyLoader
        from veronica_core.policy.metrics_policy import (
            MetricsDrivenPolicy,
            set_default_ingester,
        )

        yaml_content = """
version: "1.0"
name: test-metric-policy
rules:
  - type: metric_rule
    params:
      rules:
        - metric: total_cost_usd
          operator: gt
          threshold: 1.0
          action: halt
          agent_id: bot
    on_exceed: halt
"""
        set_default_ingester(None)
        loader = PolicyLoader()
        loaded = loader.load_from_string(yaml_content, format="yaml")
        # LoadedPolicy hooks list should have one entry
        assert len(loaded.hooks) == 1
        rule_schema, component = loaded.hooks[0]
        assert rule_schema.type == "metric_rule"
        assert isinstance(component, MetricsDrivenPolicy)
        assert component.policy_type == "metric_rule"
        assert len(component.rules) == 1
        assert component.rules[0].metric == "total_cost_usd"

    def test_metric_rule_json_load(self) -> None:
        import json
        from veronica_core.policy.loader import PolicyLoader
        from veronica_core.policy.metrics_policy import (
            MetricsDrivenPolicy,
            set_default_ingester,
        )

        json_content = json.dumps(
            {
                "version": "1.0",
                "name": "test-json-metric-policy",
                "rules": [
                    {
                        "type": "metric_rule",
                        "params": {
                            "rules": [
                                {
                                    "metric": "error_rate",
                                    "operator": "gt",
                                    "threshold": 0.3,
                                    "action": "degrade",
                                }
                            ]
                        },
                        "on_exceed": "degrade",
                    }
                ],
            }
        )
        set_default_ingester(None)
        loader = PolicyLoader()
        loaded = loader.load_from_string(json_content, format="json")
        assert len(loaded.hooks) == 1
        _, component = loaded.hooks[0]
        assert isinstance(component, MetricsDrivenPolicy)
        assert component.rules[0].metric == "error_rate"
        assert component.rules[0].action == "degrade"

    def test_metric_rule_yaml_with_ingester_check(self) -> None:
        """Full round-trip: YAML load → set ingester → check triggers correctly."""
        from veronica_core.otel_feedback import OTelMetricsIngester
        from veronica_core.policy.loader import PolicyLoader
        from veronica_core.policy.metrics_policy import (
            set_default_ingester,
        )

        yaml_content = """
version: "1.0"
name: live-test
rules:
  - type: metric_rule
    params:
      rules:
        - metric: total_cost_usd
          operator: gt
          threshold: 0.5
          action: halt
          agent_id: live-agent
    on_exceed: halt
"""
        ing = OTelMetricsIngester()
        ing.ingest_span(
            {
                "agent_id": "live-agent",
                "attributes": {"veronica.cost_usd": 1.0},
            }
        )
        set_default_ingester(ing)
        try:
            loader = PolicyLoader()
            loaded = loader.load_from_string(yaml_content, format="yaml")
            _, component = loaded.hooks[0]
            d = component.check(PolicyContext(entity_id="live-agent"))
            assert d.allowed is False
        finally:
            set_default_ingester(None)

    def test_metric_rule_multiple_rules_yaml(self) -> None:
        """Multiple rules in a single metric_rule params block."""
        from veronica_core.policy.loader import PolicyLoader
        from veronica_core.policy.metrics_policy import (
            MetricsDrivenPolicy,
            set_default_ingester,
        )

        yaml_content = """
version: "1.0"
name: multi-rule
rules:
  - type: metric_rule
    params:
      rules:
        - metric: error_rate
          operator: gt
          threshold: 0.3
          action: degrade
        - metric: avg_latency_ms
          operator: gt
          threshold: 5000
          action: warn
    on_exceed: degrade
"""
        set_default_ingester(None)
        loader = PolicyLoader()
        loaded = loader.load_from_string(yaml_content, format="yaml")
        _, component = loaded.hooks[0]
        assert isinstance(component, MetricsDrivenPolicy)
        assert len(component.rules) == 2
        assert component.rules[0].action == "degrade"
        assert component.rules[1].action == "warn"
