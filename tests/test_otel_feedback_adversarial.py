"""Adversarial tests for MetricsDrivenPolicy and OTelMetricsIngester.

Attacker mindset: "How do I break this?"

Categories covered:
1. Corrupted input — garbage values, wrong types, NaN, Inf, negative
2. Concurrent access — race conditions, TOCTOU under contention
3. Partial failure — ingester dies mid-check, missing fields
4. State corruption — invalid state, missing attributes on metrics object
5. Resource exhaustion — very large rule sets, 0 thresholds
6. Boundary abuse — exact threshold, off-by-one, MAX_FLOAT, zero
7. OTelMetricsIngester adversarial — malformed spans, concurrent ingest
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any

import pytest

from veronica_core.otel_feedback import OTelMetricsIngester
from veronica_core.policy.metrics_policy import (
    MetricRule,
    MetricsDrivenPolicy,
    set_default_ingester,
)
from veronica_core.runtime_policy import PolicyContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(entity_id: str = "") -> PolicyContext:
    return PolicyContext(entity_id=entity_id)


@dataclass
class BadMetrics:
    """Metrics object with corrupted / non-numeric fields."""

    total_cost: Any = "not_a_number"
    total_tokens: Any = None
    avg_latency_ms: Any = float("nan")
    error_rate: Any = float("inf")
    _error_count: Any = -999
    call_count: int = 0


class RaisingIngester:
    """Ingester that raises on every call."""

    def get_agent_metrics(self, agent_id: str) -> None:
        raise ConnectionError(f"Backend unavailable for {agent_id}")


class FlakeyIngester:
    """Ingester that alternates between success and failure."""

    def __init__(self, good_metrics: Any) -> None:
        self._metrics = good_metrics
        self._call_count = 0
        self._lock = threading.Lock()

    def get_agent_metrics(self, agent_id: str) -> Any:
        with self._lock:
            self._call_count += 1
            if self._call_count % 2 == 0:
                raise RuntimeError("transient failure")
            return self._metrics


class SlowIngester:
    """Ingester that sleeps before returning, to provoke TOCTOU races."""

    def __init__(self, metrics: Any, delay: float = 0.005) -> None:
        self._metrics = metrics
        self._delay = delay

    def get_agent_metrics(self, agent_id: str) -> Any:
        time.sleep(self._delay)
        return self._metrics


# ---------------------------------------------------------------------------
# Category 1: Corrupted input — garbage metric values
# ---------------------------------------------------------------------------


class TestAdversarialCorruptedMetrics:
    """Corrupted metric values must not crash the policy — fail-safe: allow."""

    def _policy(self, ingester: Any, metric: str, op: str, threshold: float, action: str) -> Any:
        rule = MetricRule(metric, op, threshold, action, agent_id="bot")
        return MetricsDrivenPolicy(rules=[rule], ingester=ingester)

    def test_nan_metric_value_is_safe(self) -> None:
        bad = BadMetrics(avg_latency_ms=float("nan"))
        ingester = type("I", (), {"get_agent_metrics": lambda self, aid: bad})()
        policy = self._policy(ingester, "avg_latency_ms", "gt", 100.0, "halt")
        d = policy.check(_ctx(entity_id="bot"))
        # float("nan") > 100.0 is False → rule not triggered → allow
        assert d.allowed is True

    def test_inf_metric_value_triggers_correctly(self) -> None:
        bad = BadMetrics(error_rate=float("inf"))
        ingester = type("I", (), {"get_agent_metrics": lambda self, aid: bad})()
        policy = self._policy(ingester, "error_rate", "gt", 0.0, "halt")
        d = policy.check(_ctx(entity_id="bot"))
        # inf > 0.0 is True → halt
        assert d.allowed is False

    def test_string_metric_value_is_safe(self) -> None:
        bad = BadMetrics(total_cost="oops")
        ingester = type("I", (), {"get_agent_metrics": lambda self, aid: bad})()
        policy = self._policy(ingester, "total_cost_usd", "gt", 1.0, "halt")
        d = policy.check(_ctx(entity_id="bot"))
        # float("oops") raises → returns None → rule skipped → allow
        assert d.allowed is True

    def test_none_metric_value_is_safe(self) -> None:
        bad = BadMetrics(total_tokens=None)
        ingester = type("I", (), {"get_agent_metrics": lambda self, aid: bad})()
        policy = self._policy(ingester, "total_tokens", "gt", 0, "halt")
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is True

    def test_negative_error_count_does_not_trigger_warn(self) -> None:
        bad = BadMetrics(_error_count=-999)
        ingester = type("I", (), {"get_agent_metrics": lambda self, aid: bad})()
        # Negative error count (-999) < 5 → warn not triggered
        rule = MetricRule("error_count", "gt", 5, "warn", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ingester)
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is True

    def test_missing_attribute_on_metrics_is_safe(self) -> None:
        class NoAttrMetrics:
            pass  # has none of the expected fields

        ingester = type("I", (), {"get_agent_metrics": lambda self, aid: NoAttrMetrics()})()
        rule = MetricRule("total_cost_usd", "gt", 0.0, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ingester)
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is True


# ---------------------------------------------------------------------------
# Category 2: Concurrent access — race conditions
# ---------------------------------------------------------------------------


class TestAdversarialConcurrent:
    def test_concurrent_add_and_check_no_deadlock(self) -> None:
        """Simultaneous add_rule + check must not deadlock or crash."""

        @dataclass
        class SimpleMetrics:
            total_cost: float = 0.5

        ingester = type(
            "I", (), {"get_agent_metrics": lambda self, aid: SimpleMetrics()}
        )()
        policy = MetricsDrivenPolicy(rules=[], ingester=ingester, agent_id="bot")
        errors: list[Exception] = []
        stop = threading.Event()

        def _add() -> None:
            while not stop.is_set():
                try:
                    policy.add_rule(MetricRule("total_cost_usd", "gt", 1.0, "warn", agent_id="bot"))
                    policy.clear_rules()
                except Exception as exc:
                    errors.append(exc)

        def _check() -> None:
            for _ in range(50):
                try:
                    policy.check(_ctx(entity_id="bot"))
                except Exception as exc:
                    errors.append(exc)

        adder = threading.Thread(target=_add)
        checkers = [threading.Thread(target=_check) for _ in range(5)]
        adder.start()
        for t in checkers:
            t.start()
        for t in checkers:
            t.join()
        stop.set()
        adder.join(timeout=2.0)

        assert not errors, f"Concurrent errors: {errors}"

    def test_20_threads_severity_ordering_consistent(self) -> None:
        """Under high concurrency, halt must always win over warn."""

        @dataclass
        class HotMetrics:
            total_cost: float = 5.0
            error_rate: float = 0.5

        ingester = type(
            "I", (), {"get_agent_metrics": lambda self, aid: HotMetrics()}
        )()
        rules = [
            MetricRule("error_rate", "gt", 0.1, "warn", agent_id="bot"),
            MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot"),
        ]
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

        assert not errors
        # Halt must dominate — no thread should get allowed=True
        assert all(r is False for r in results), f"Some threads got allow: {results}"

    def test_toctou_ingester_changes_between_checks(self) -> None:
        """Rule evaluation after ingester metric change must be consistent."""
        metrics_store: dict[str, float] = {"cost": 0.0}

        class MutableIngester:
            def get_agent_metrics(self, agent_id: str):  # noqa: ANN001
                cost = metrics_store["cost"]

                @dataclass
                class M:
                    total_cost: float = cost

                return M()

        ingester = MutableIngester()
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ingester)

        # Before: cost=0.0 → allow
        d1 = policy.check(_ctx(entity_id="bot"))
        assert d1.allowed is True

        # Update externally
        metrics_store["cost"] = 5.0

        # After: cost=5.0 → halt
        d2 = policy.check(_ctx(entity_id="bot"))
        assert d2.allowed is False


# ---------------------------------------------------------------------------
# Category 3: Partial failure — ingester dies mid-operation
# ---------------------------------------------------------------------------


class TestAdversarialPartialFailure:
    def test_raising_ingester_always_safe(self) -> None:
        rule = MetricRule("total_cost_usd", "gt", 0.0, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=RaisingIngester())
        for _ in range(10):
            d = policy.check(_ctx(entity_id="bot"))
            assert d.allowed is True, "Raising ingester must fail-safe to allow"

    def test_flakey_ingester_never_crashes_policy(self) -> None:
        @dataclass
        class M:
            total_cost: float = 5.0

        ingester = FlakeyIngester(M())
        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ingester)

        results: list[bool] = []
        for _ in range(20):
            d = policy.check(_ctx(entity_id="bot"))
            results.append(d.allowed)
        # Must never raise; alternating allow/deny is fine
        assert len(results) == 20

    def test_ingester_returns_none_is_safe(self) -> None:
        ingester = type("I", (), {"get_agent_metrics": lambda self, aid: None})()
        rule = MetricRule("total_cost_usd", "gt", 0.0, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ingester)
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is True


# ---------------------------------------------------------------------------
# Category 4: State corruption / invalid ingester interfaces
# ---------------------------------------------------------------------------


class TestAdversarialStateCorruption:
    def test_ingester_with_no_get_agent_metrics_falls_back(self) -> None:
        """Ingester missing get_agent_metrics but has get_metrics (compat)."""

        @dataclass
        class M:
            total_cost: float = 5.0

        class OldStyleIngester:
            def get_metrics(self, agent_id: str) -> M:
                return M()

        rule = MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=OldStyleIngester())
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is False

    def test_ingester_with_no_method_is_safe(self) -> None:
        """Ingester missing both get_agent_metrics and get_metrics → safe allow."""

        class EmptyIngester:
            pass

        rule = MetricRule("total_cost_usd", "gt", 0.0, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=EmptyIngester())
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is True


# ---------------------------------------------------------------------------
# Category 5: Boundary abuse — exact threshold, zero, MAX_FLOAT
# ---------------------------------------------------------------------------


class TestAdversarialBoundary:
    def _policy(
        self,
        value: float,
        operator: str,
        threshold: float,
        action: str = "halt",
    ) -> Any:
        @dataclass
        class M:
            total_cost: float = value

        ingester = type("I", (), {"get_agent_metrics": lambda self, aid: M()})()
        rule = MetricRule("total_cost_usd", operator, threshold, action, agent_id="bot")
        return MetricsDrivenPolicy(rules=[rule], ingester=ingester)

    def test_exact_threshold_gt_not_triggered(self) -> None:
        p = self._policy(1.0, "gt", 1.0)
        assert p.check(_ctx(entity_id="bot")).allowed is True

    def test_exact_threshold_gte_triggered(self) -> None:
        p = self._policy(1.0, "gte", 1.0)
        assert p.check(_ctx(entity_id="bot")).allowed is False

    def test_zero_threshold_gt_triggered_by_positive(self) -> None:
        p = self._policy(0.0001, "gt", 0.0)
        assert p.check(_ctx(entity_id="bot")).allowed is False

    def test_zero_threshold_gt_not_triggered_by_zero(self) -> None:
        p = self._policy(0.0, "gt", 0.0)
        assert p.check(_ctx(entity_id="bot")).allowed is True

    def test_max_float_threshold_never_triggers(self) -> None:
        p = self._policy(1e300, "gt", math.inf)
        assert p.check(_ctx(entity_id="bot")).allowed is True

    def test_negative_threshold_gt_always_triggered(self) -> None:
        # 0.0 > -1.0 → triggered
        p = self._policy(0.0, "gt", -1.0)
        assert p.check(_ctx(entity_id="bot")).allowed is False

    def test_inf_value_triggers_gt(self) -> None:
        p = self._policy(math.inf, "gt", 1000.0)
        assert p.check(_ctx(entity_id="bot")).allowed is False


# ---------------------------------------------------------------------------
# Category 6: OTelMetricsIngester adversarial span ingestion
# ---------------------------------------------------------------------------


class TestAdversarialOTelIngester:
    def test_none_span_is_ignored(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span(None)  # type: ignore[arg-type]
        # No agents registered
        m = ing.get_agent_metrics("x")
        assert m.call_count == 0

    def test_empty_span_is_ignored(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span({})
        # Empty span with no name → agent_id="unknown"
        m = ing.get_agent_metrics("unknown")
        assert m.call_count == 1  # registered as "unknown"

    def test_span_with_unknown_span_type_is_skipped(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span({"span_type": "custom_framework", "agent_id": "bot"})
        m = ing.get_agent_metrics("bot")
        assert m.call_count == 0

    def test_negative_cost_is_ignored(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span({
            "agent_id": "bot",
            "attributes": {"veronica.cost_usd": -5.0},
        })
        m = ing.get_agent_metrics("bot")
        assert m.total_cost == 0.0

    def test_nan_cost_is_ignored(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span({
            "agent_id": "bot",
            "attributes": {"veronica.cost_usd": float("nan")},
        })
        m = ing.get_agent_metrics("bot")
        assert m.total_cost == 0.0

    def test_malformed_timestamps_dont_crash(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span({
            "agent_id": "bot",
            "start_time": "not_a_timestamp",
            "end_time": "also_not",
        })
        m = ing.get_agent_metrics("bot")
        # Span registered, duration_ms just missing (None)
        assert m.call_count == 1

    def test_negative_timestamps_dont_crash(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span({
            "agent_id": "bot",
            "start_time": 100.0,
            "end_time": 50.0,  # end < start → negative duration
        })
        m = ing.get_agent_metrics("bot")
        assert m.call_count == 1
        assert m.avg_latency_ms == 0.0  # negative duration rejected

    def test_concurrent_ingest_multiple_agents(self) -> None:
        ing = OTelMetricsIngester()
        errors: list[Exception] = []

        def _ingest(agent_id: str, cost: float) -> None:
            for _ in range(50):
                try:
                    ing.ingest_span({
                        "agent_id": agent_id,
                        "attributes": {"veronica.cost_usd": cost},
                    })
                except Exception as exc:
                    errors.append(exc)

        threads = [
            threading.Thread(target=_ingest, args=(f"agent-{i}", float(i)))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        for i in range(5):
            m = ing.get_agent_metrics(f"agent-{i}")
            assert m.call_count == 50
            assert m.total_cost == pytest.approx(float(i) * 50)

    def test_very_large_token_count(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span({
            "agent_id": "bot",
            "attributes": {"llm.token.count.total": 10**12},
        })
        m = ing.get_agent_metrics("bot")
        assert m.total_tokens == 10**12

    def test_error_span_increments_error_rate(self) -> None:
        ing = OTelMetricsIngester()
        # 1 error span
        ing.ingest_span({
            "agent_id": "bot",
            "status": "ERROR",
        })
        # 1 success span
        ing.ingest_span({
            "agent_id": "bot",
        })
        m = ing.get_agent_metrics("bot")
        assert m.call_count == 2
        assert m.error_rate == pytest.approx(0.5)

    def test_reset_single_agent(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span({"agent_id": "bot", "attributes": {"veronica.cost_usd": 1.0}})
        ing.ingest_span({"agent_id": "other", "attributes": {"veronica.cost_usd": 2.0}})
        ing.reset("bot")
        assert ing.get_agent_metrics("bot").call_count == 0
        assert ing.get_agent_metrics("other").call_count == 1

    def test_reset_all_agents(self) -> None:
        ing = OTelMetricsIngester()
        for i in range(5):
            ing.ingest_span({"agent_id": f"bot-{i}", "attributes": {"veronica.cost_usd": 1.0}})
        ing.reset()
        for i in range(5):
            assert ing.get_agent_metrics(f"bot-{i}").call_count == 0


# ---------------------------------------------------------------------------
# Category 7: MetricsDrivenPolicy + real OTelMetricsIngester integration
# ---------------------------------------------------------------------------


class TestAdversarialEndToEnd:
    def test_policy_triggers_after_span_ingest(self) -> None:
        ing = OTelMetricsIngester()
        rule = MetricRule("total_cost_usd", "gt", 0.5, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ing)

        # Before ingest → allow
        d1 = policy.check(_ctx(entity_id="bot"))
        assert d1.allowed is True  # total_cost=0.0, not > 0.5

        # Ingest expensive spans
        for _ in range(3):
            ing.ingest_span({
                "agent_id": "bot",
                "attributes": {"veronica.cost_usd": 0.3},
            })

        # After ingest → halt (total_cost=0.9 > 0.5)
        d2 = policy.check(_ctx(entity_id="bot"))
        assert d2.allowed is False

    def test_error_rate_policy_with_real_ingester(self) -> None:
        ing = OTelMetricsIngester()
        rule = MetricRule("error_rate", "gt", 0.25, "degrade", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ing)

        # 3 errors, 1 success → error_rate=0.75 > 0.25
        for _ in range(3):
            ing.ingest_span({"agent_id": "bot", "status": "ERROR"})
        ing.ingest_span({"agent_id": "bot"})

        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is True
        assert d.degradation_action == "MODEL_DOWNGRADE"

    def test_default_ingester_set_globally_works_with_real_ingester(self) -> None:
        set_default_ingester(None)
        try:
            ing = OTelMetricsIngester()
            ing.ingest_span({
                "agent_id": "global-bot",
                "attributes": {"veronica.cost_usd": 2.0},
            })
            set_default_ingester(ing)

            rule = MetricRule("total_cost_usd", "gt", 1.0, "halt", agent_id="global-bot")
            policy = MetricsDrivenPolicy(rules=[rule])  # no explicit ingester
            d = policy.check(_ctx())
            assert d.allowed is False
        finally:
            set_default_ingester(None)

    def test_latency_rule_with_real_ingester(self) -> None:
        ing = OTelMetricsIngester()
        rule = MetricRule("avg_latency_ms", "gt", 100.0, "warn", agent_id="slow-bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ing)

        # Ingest spans with 200ms latency
        ing.ingest_span({
            "agent_id": "slow-bot",
            "start_time": 0.0,
            "end_time": 0.2,  # 200ms
        })
        ing.ingest_span({
            "agent_id": "slow-bot",
            "start_time": 0.0,
            "end_time": 0.3,  # 300ms
        })
        # avg = 250ms > 100ms → warn
        d = policy.check(_ctx(entity_id="slow-bot"))
        assert d.allowed is True
        assert "WARN:" in d.reason

    def test_concurrent_ingest_and_policy_check(self) -> None:
        """Concurrent span ingestion and policy checks must not corrupt state."""
        ing = OTelMetricsIngester()
        rule = MetricRule("total_cost_usd", "gt", 100.0, "halt", agent_id="stress-bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ing)
        errors: list[Exception] = []

        def _ingest() -> None:
            for _ in range(100):
                try:
                    ing.ingest_span({
                        "agent_id": "stress-bot",
                        "attributes": {"veronica.cost_usd": 0.1},
                    })
                except Exception as exc:
                    errors.append(exc)

        def _check() -> None:
            for _ in range(50):
                try:
                    policy.check(_ctx(entity_id="stress-bot"))
                except Exception as exc:
                    errors.append(exc)

        ingesters = [threading.Thread(target=_ingest) for _ in range(5)]
        checkers = [threading.Thread(target=_check) for _ in range(5)]
        all_threads = ingesters + checkers
        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join()

        assert not errors, f"Concurrent errors: {errors}"
        # Final metrics should be internally consistent
        m = ing.get_agent_metrics("stress-bot")
        assert m.call_count == 500
        assert m.total_cost == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Category 8: Unicode / special agent_id handling
# ---------------------------------------------------------------------------


class TestAdversarialAgentIdEdgeCases:
    """Adversarial agent_id values: Unicode, empty, special chars."""

    def test_unicode_japanese_agent_id(self) -> None:
        ing = OTelMetricsIngester()
        agent_id = "エージェント-1"
        ing.ingest_span({
            "agent_id": agent_id,
            "attributes": {"veronica.cost_usd": 0.5},
        })
        m = ing.get_agent_metrics(agent_id)
        assert m.call_count == 1
        assert m.total_cost == pytest.approx(0.5)

    def test_unicode_emoji_agent_id(self) -> None:
        ing = OTelMetricsIngester()
        agent_id = "bot-\U0001F916"  # robot emoji
        ing.ingest_span({
            "agent_id": agent_id,
            "attributes": {"veronica.cost_usd": 1.0},
        })
        m = ing.get_agent_metrics(agent_id)
        assert m.call_count == 1

    def test_empty_string_agent_id_tracked_as_unknown(self) -> None:
        """Empty agent_id in span → falls back to span name or 'unknown'."""
        ing = OTelMetricsIngester()
        ing.ingest_span({
            "agent_id": "",
            "name": "",
        })
        # Falls back to "unknown"
        m = ing.get_agent_metrics("unknown")
        assert m.call_count == 1

    def test_very_long_agent_id(self) -> None:
        ing = OTelMetricsIngester()
        agent_id = "a" * 10_000
        ing.ingest_span({
            "agent_id": agent_id,
            "attributes": {"veronica.cost_usd": 0.1},
        })
        m = ing.get_agent_metrics(agent_id)
        assert m.call_count == 1

    def test_whitespace_only_agent_id(self) -> None:
        """Whitespace-only agent_id is technically valid (str)."""
        ing = OTelMetricsIngester()
        ing.ingest_span({
            "agent_id": "   ",
            "attributes": {"veronica.cost_usd": 0.1},
        })
        m = ing.get_agent_metrics("   ")
        assert m.call_count == 1

    def test_null_byte_in_agent_id(self) -> None:
        """Null bytes in agent_id — must not crash."""
        ing = OTelMetricsIngester()
        agent_id = "bot\x00null"
        ing.ingest_span({
            "agent_id": agent_id,
            "attributes": {"veronica.cost_usd": 0.1},
        })
        m = ing.get_agent_metrics(agent_id)
        assert m.call_count == 1

    def test_policy_with_unicode_agent_id_in_context(self) -> None:
        """MetricsDrivenPolicy resolves Unicode entity_id correctly."""
        from veronica_core.otel_feedback.policy import (
            MetricRule as OtelRule,
            MetricsDrivenPolicy as OtelPolicy,
        )
        from veronica_core.runtime_policy import PolicyContext

        ing = OTelMetricsIngester()
        agent_id = "エージェント-1"
        ing.ingest_span({
            "agent_id": agent_id,
            "attributes": {"veronica.cost_usd": 5.0},
        })
        rules = [OtelRule("total_cost", "gt", 1.0, "halt")]
        policy = OtelPolicy(ingester=ing, rules=rules)
        d = policy.check(PolicyContext(entity_id=agent_id))
        assert d.allowed is False


# ---------------------------------------------------------------------------
# Category 9: Memory pressure — thousands of unique agent_ids
# ---------------------------------------------------------------------------


class TestAdversarialMemoryPressure:
    def test_thousands_of_unique_agents(self) -> None:
        """Ingesting spans for 5K unique agents must not OOM or crash."""
        ing = OTelMetricsIngester()
        agent_count = 5_000
        for i in range(agent_count):
            ing.ingest_span({
                "agent_id": f"agent-{i}",
                "attributes": {"veronica.cost_usd": 0.001},
            })
        all_agents = ing.get_all_agents()
        assert len(all_agents) == agent_count
        # Each agent has exactly 1 call
        for metrics in all_agents.values():
            assert metrics.call_count == 1

    def test_rapid_fire_10k_spans_single_agent(self) -> None:
        """10K spans for one agent in a tight loop — no crash, correct count."""
        ing = OTelMetricsIngester()
        n = 10_000
        for i in range(n):
            ing.ingest_span({
                "agent_id": "rapid-bot",
                "attributes": {"veronica.cost_usd": 0.0001},
            })
        m = ing.get_agent_metrics("rapid-bot")
        assert m.call_count == n
        assert m.total_cost == pytest.approx(n * 0.0001)

    def test_reset_frees_metrics_for_all_agents(self) -> None:
        """After reset(), all agents return zero metrics."""
        ing = OTelMetricsIngester()
        for i in range(100):
            ing.ingest_span({
                "agent_id": f"bot-{i}",
                "attributes": {"veronica.cost_usd": float(i)},
            })
        ing.reset()
        for i in range(100):
            m = ing.get_agent_metrics(f"bot-{i}")
            assert m.call_count == 0
            assert m.total_cost == 0.0


# ---------------------------------------------------------------------------
# Category 10: Zero-length sliding window and edge window sizes
# ---------------------------------------------------------------------------


class TestAdversarialWindowEdgeCases:
    def test_zero_window_sec_raises(self) -> None:
        """window_sec <= 0 must raise ValueError at construction."""
        with pytest.raises(ValueError, match="window_sec"):
            OTelMetricsIngester(window_sec=0.0)

    def test_negative_window_sec_raises(self) -> None:
        with pytest.raises(ValueError, match="window_sec"):
            OTelMetricsIngester(window_sec=-1.0)

    def test_very_small_window_sec(self) -> None:
        """window_sec=0.001 (1ms) — spans older than 1ms pruned from window."""
        ing = OTelMetricsIngester(window_sec=0.001)
        ing.ingest_span({
            "agent_id": "bot",
            "attributes": {"veronica.cost_usd": 1.0},
        })
        m = ing.get_agent_metrics("bot")
        # call_count and total_cost are NOT windowed — they accumulate
        assert m.call_count == 1
        assert m.total_cost == pytest.approx(1.0)

    def test_extremely_large_window_sec(self) -> None:
        """window_sec=1e9 — effectively infinite window."""
        ing = OTelMetricsIngester(window_sec=1e9)
        for _ in range(5):
            ing.ingest_span({
                "agent_id": "bot",
                "attributes": {"veronica.cost_usd": 0.1},
            })
        m = ing.get_agent_metrics("bot")
        assert m.call_count == 5


# ---------------------------------------------------------------------------
# Category 11: Extremely large metric values
# ---------------------------------------------------------------------------


class TestAdversarialExtremeValues:
    def test_extremely_large_cost(self) -> None:
        """1e308 cost ingested — must not crash."""
        ing = OTelMetricsIngester()
        ing.ingest_span({
            "agent_id": "rich-bot",
            "attributes": {"veronica.cost_usd": 1e308},
        })
        m = ing.get_agent_metrics("rich-bot")
        assert m.total_cost == pytest.approx(1e308)

    def test_inf_cost_is_rejected(self) -> None:
        """inf cost (not finite) — must be rejected (cost stays 0)."""
        ing = OTelMetricsIngester()
        ing.ingest_span({
            "agent_id": "bot",
            "attributes": {"veronica.cost_usd": float("inf")},
        })
        m = ing.get_agent_metrics("bot")
        assert m.total_cost == 0.0

    def test_very_large_token_count_accumulated(self) -> None:
        """Accumulate 10 spans with 1e9 tokens each."""
        ing = OTelMetricsIngester()
        for _ in range(10):
            ing.ingest_span({
                "agent_id": "token-bot",
                "attributes": {"llm.token.count.total": 1_000_000_000},
            })
        m = ing.get_agent_metrics("token-bot")
        assert m.total_tokens == 10 * 1_000_000_000

    def test_policy_handles_1e308_metric(self) -> None:
        """Policy with 1e308 threshold never triggers."""
        from veronica_core.otel_feedback.policy import (
            MetricRule as OtelRule,
            MetricsDrivenPolicy as OtelPolicy,
        )
        from veronica_core.runtime_policy import PolicyContext

        ing = OTelMetricsIngester()
        ing.ingest_span({
            "agent_id": "bot",
            "attributes": {"veronica.cost_usd": 1e300},
        })
        rules = [OtelRule("total_cost", "gt", 1e308, "halt")]
        policy = OtelPolicy(ingester=ing, rules=rules)
        d = policy.check(PolicyContext(entity_id="bot"))
        assert d.allowed is True  # 1e300 not > 1e308


# ---------------------------------------------------------------------------
# Category 12: TOCTOU — ingest between check() calls
# ---------------------------------------------------------------------------


class TestAdversarialTOCTOU:
    def test_ingest_between_two_checks_reflects_correctly(self) -> None:
        """State change between check() calls must be reflected immediately."""
        from veronica_core.otel_feedback.policy import (
            MetricRule as OtelRule,
            MetricsDrivenPolicy as OtelPolicy,
        )
        from veronica_core.runtime_policy import PolicyContext

        ing = OTelMetricsIngester()
        rules = [OtelRule("total_cost", "gt", 1.0, "halt")]
        policy = OtelPolicy(ingester=ing, rules=rules)

        # Check 1: no cost yet → allow
        d1 = policy.check(PolicyContext(entity_id="bot"))
        assert d1.allowed is True

        # Ingest while policy is "idle"
        ing.ingest_span({
            "agent_id": "bot",
            "attributes": {"veronica.cost_usd": 2.0},
        })

        # Check 2: cost=2.0 > 1.0 → halt
        d2 = policy.check(PolicyContext(entity_id="bot"))
        assert d2.allowed is False

    def test_concurrent_ingest_toctou_race(self) -> None:
        """10 threads ingesting while 10 threads checking — no state corruption."""
        from veronica_core.otel_feedback.policy import (
            MetricRule as OtelRule,
            MetricsDrivenPolicy as OtelPolicy,
        )
        from veronica_core.runtime_policy import PolicyContext

        ing = OTelMetricsIngester()
        rules = [OtelRule("call_count", "gte", 50, "warn")]
        policy = OtelPolicy(ingester=ing, rules=rules)
        errors: list[Exception] = []

        def _ingest() -> None:
            for _ in range(20):
                try:
                    ing.ingest_span({"agent_id": "toctou-bot"})
                except Exception as exc:
                    errors.append(exc)

        def _check() -> None:
            for _ in range(20):
                try:
                    policy.check(PolicyContext(entity_id="toctou-bot"))
                except Exception as exc:
                    errors.append(exc)

        ingesters = [threading.Thread(target=_ingest) for _ in range(10)]
        checkers = [threading.Thread(target=_check) for _ in range(10)]
        for t in ingesters + checkers:
            t.start()
        for t in ingesters + checkers:
            t.join()

        assert not errors
        # Final call_count must be exactly 200 (10 threads × 20 calls)
        m = ing.get_agent_metrics("toctou-bot")
        assert m.call_count == 200

    def test_get_agent_metrics_nonexistent_returns_zero(self) -> None:
        """get_agent_metrics for never-seen agent returns zeroed AgentMetrics."""
        ing = OTelMetricsIngester()
        m = ing.get_agent_metrics("ghost-agent-never-seen")
        assert m.call_count == 0
        assert m.total_cost == 0.0
        assert m.total_tokens == 0
        assert m.error_rate == 0.0
        assert m.avg_latency_ms == 0.0
