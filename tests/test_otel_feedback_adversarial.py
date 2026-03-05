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

    def _policy(
        self, ingester: Any, metric: str, op: str, threshold: float, action: str
    ) -> Any:
        rule = MetricRule(metric, op, threshold, action, agent_id="bot")
        return MetricsDrivenPolicy(rules=[rule], ingester=ingester)

    def test_nan_metric_value_is_safe(self) -> None:
        bad = BadMetrics(avg_latency_ms=float("nan"))
        ingester = type("I", (), {"get_agent_metrics": lambda self, aid: bad})()
        policy = self._policy(ingester, "avg_latency_ms", "gt", 100.0, "halt")
        d = policy.check(_ctx(entity_id="bot"))
        # float("nan") > 100.0 is False → rule not triggered → allow
        assert d.allowed is True

    def test_inf_metric_value_is_skipped(self) -> None:
        """Non-finite observed values (inf) are filtered out — rule is skipped."""
        bad = BadMetrics(error_rate=float("inf"))
        ingester = type("I", (), {"get_agent_metrics": lambda self, aid: bad})()
        policy = self._policy(ingester, "error_rate", "gt", 0.0, "halt")
        d = policy.check(_ctx(entity_id="bot"))
        # inf is non-finite → _extract_metric returns None → rule skipped → allow
        assert d.allowed is True

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

        ingester = type(
            "I", (), {"get_agent_metrics": lambda self, aid: NoAttrMetrics()}
        )()
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
                    policy.add_rule(
                        MetricRule("total_cost_usd", "gt", 1.0, "warn", agent_id="bot")
                    )
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
        # Use the largest finite float (1e308) rather than inf — inf is now rejected
        # by MetricRule validation (non-finite threshold is a configuration error).
        p = self._policy(1e300, "gt", 1e308)
        assert p.check(_ctx(entity_id="bot")).allowed is True

    def test_negative_threshold_gt_always_triggered(self) -> None:
        # 0.0 > -1.0 → triggered
        p = self._policy(0.0, "gt", -1.0)
        assert p.check(_ctx(entity_id="bot")).allowed is False

    def test_inf_observed_value_is_skipped(self) -> None:
        """Non-finite observed values (inf) are filtered — rule skipped, allow."""
        p = self._policy(math.inf, "gt", 1000.0)
        assert p.check(_ctx(entity_id="bot")).allowed is True


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
        ing.ingest_span(
            {
                "agent_id": "bot",
                "attributes": {"veronica.cost_usd": -5.0},
            }
        )
        m = ing.get_agent_metrics("bot")
        assert m.total_cost == 0.0

    def test_nan_cost_is_ignored(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span(
            {
                "agent_id": "bot",
                "attributes": {"veronica.cost_usd": float("nan")},
            }
        )
        m = ing.get_agent_metrics("bot")
        assert m.total_cost == 0.0

    def test_malformed_timestamps_dont_crash(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span(
            {
                "agent_id": "bot",
                "start_time": "not_a_timestamp",
                "end_time": "also_not",
            }
        )
        m = ing.get_agent_metrics("bot")
        # Span registered, duration_ms just missing (None)
        assert m.call_count == 1

    def test_negative_timestamps_dont_crash(self) -> None:
        ing = OTelMetricsIngester()
        ing.ingest_span(
            {
                "agent_id": "bot",
                "start_time": 100.0,
                "end_time": 50.0,  # end < start → negative duration
            }
        )
        m = ing.get_agent_metrics("bot")
        assert m.call_count == 1
        assert m.avg_latency_ms == 0.0  # negative duration rejected

    def test_concurrent_ingest_multiple_agents(self) -> None:
        ing = OTelMetricsIngester()
        errors: list[Exception] = []

        def _ingest(agent_id: str, cost: float) -> None:
            for _ in range(50):
                try:
                    ing.ingest_span(
                        {
                            "agent_id": agent_id,
                            "attributes": {"veronica.cost_usd": cost},
                        }
                    )
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
        ing.ingest_span(
            {
                "agent_id": "bot",
                "attributes": {"llm.token.count.total": 10**12},
            }
        )
        m = ing.get_agent_metrics("bot")
        assert m.total_tokens == 10**12

    def test_error_span_increments_error_rate(self) -> None:
        ing = OTelMetricsIngester()
        # 1 error span
        ing.ingest_span(
            {
                "agent_id": "bot",
                "status": "ERROR",
            }
        )
        # 1 success span
        ing.ingest_span(
            {
                "agent_id": "bot",
            }
        )
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
            ing.ingest_span(
                {"agent_id": f"bot-{i}", "attributes": {"veronica.cost_usd": 1.0}}
            )
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
            ing.ingest_span(
                {
                    "agent_id": "bot",
                    "attributes": {"veronica.cost_usd": 0.3},
                }
            )

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
            ing.ingest_span(
                {
                    "agent_id": "global-bot",
                    "attributes": {"veronica.cost_usd": 2.0},
                }
            )
            set_default_ingester(ing)

            rule = MetricRule(
                "total_cost_usd", "gt", 1.0, "halt", agent_id="global-bot"
            )
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
        ing.ingest_span(
            {
                "agent_id": "slow-bot",
                "start_time": 0.0,
                "end_time": 0.2,  # 200ms
            }
        )
        ing.ingest_span(
            {
                "agent_id": "slow-bot",
                "start_time": 0.0,
                "end_time": 0.3,  # 300ms
            }
        )
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
                    ing.ingest_span(
                        {
                            "agent_id": "stress-bot",
                            "attributes": {"veronica.cost_usd": 0.1},
                        }
                    )
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
        ing.ingest_span(
            {
                "agent_id": agent_id,
                "attributes": {"veronica.cost_usd": 0.5},
            }
        )
        m = ing.get_agent_metrics(agent_id)
        assert m.call_count == 1
        assert m.total_cost == pytest.approx(0.5)

    def test_unicode_emoji_agent_id(self) -> None:
        ing = OTelMetricsIngester()
        agent_id = "bot-\U0001f916"  # robot emoji
        ing.ingest_span(
            {
                "agent_id": agent_id,
                "attributes": {"veronica.cost_usd": 1.0},
            }
        )
        m = ing.get_agent_metrics(agent_id)
        assert m.call_count == 1

    def test_empty_string_agent_id_tracked_as_unknown(self) -> None:
        """Empty agent_id in span → falls back to span name or 'unknown'."""
        ing = OTelMetricsIngester()
        ing.ingest_span(
            {
                "agent_id": "",
                "name": "",
            }
        )
        # Falls back to "unknown"
        m = ing.get_agent_metrics("unknown")
        assert m.call_count == 1

    def test_very_long_agent_id(self) -> None:
        ing = OTelMetricsIngester()
        agent_id = "a" * 10_000
        ing.ingest_span(
            {
                "agent_id": agent_id,
                "attributes": {"veronica.cost_usd": 0.1},
            }
        )
        m = ing.get_agent_metrics(agent_id)
        assert m.call_count == 1

    def test_whitespace_only_agent_id(self) -> None:
        """Whitespace-only agent_id is technically valid (str)."""
        ing = OTelMetricsIngester()
        ing.ingest_span(
            {
                "agent_id": "   ",
                "attributes": {"veronica.cost_usd": 0.1},
            }
        )
        m = ing.get_agent_metrics("   ")
        assert m.call_count == 1

    def test_null_byte_in_agent_id(self) -> None:
        """Null bytes in agent_id — must not crash."""
        ing = OTelMetricsIngester()
        agent_id = "bot\x00null"
        ing.ingest_span(
            {
                "agent_id": agent_id,
                "attributes": {"veronica.cost_usd": 0.1},
            }
        )
        m = ing.get_agent_metrics(agent_id)
        assert m.call_count == 1

    def test_policy_with_unicode_agent_id_in_context(self) -> None:
        """MetricsDrivenPolicy resolves Unicode entity_id correctly."""
        from veronica_core.policy.metrics_policy import (
            MetricRule,
            MetricsDrivenPolicy,
        )
        from veronica_core.runtime_policy import PolicyContext

        ing = OTelMetricsIngester()
        agent_id = "エージェント-1"
        ing.ingest_span(
            {
                "agent_id": agent_id,
                "attributes": {"veronica.cost_usd": 5.0},
            }
        )
        rules = [MetricRule("total_cost_usd", "gt", 1.0, "halt")]
        policy = MetricsDrivenPolicy(ingester=ing, rules=rules)
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
            ing.ingest_span(
                {
                    "agent_id": f"agent-{i}",
                    "attributes": {"veronica.cost_usd": 0.001},
                }
            )
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
            ing.ingest_span(
                {
                    "agent_id": "rapid-bot",
                    "attributes": {"veronica.cost_usd": 0.0001},
                }
            )
        m = ing.get_agent_metrics("rapid-bot")
        assert m.call_count == n
        assert m.total_cost == pytest.approx(n * 0.0001)

    def test_reset_frees_metrics_for_all_agents(self) -> None:
        """After reset(), all agents return zero metrics."""
        ing = OTelMetricsIngester()
        for i in range(100):
            ing.ingest_span(
                {
                    "agent_id": f"bot-{i}",
                    "attributes": {"veronica.cost_usd": float(i)},
                }
            )
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
        ing.ingest_span(
            {
                "agent_id": "bot",
                "attributes": {"veronica.cost_usd": 1.0},
            }
        )
        m = ing.get_agent_metrics("bot")
        # call_count and total_cost are NOT windowed — they accumulate
        assert m.call_count == 1
        assert m.total_cost == pytest.approx(1.0)

    def test_extremely_large_window_sec(self) -> None:
        """window_sec=1e9 — effectively infinite window."""
        ing = OTelMetricsIngester(window_sec=1e9)
        for _ in range(5):
            ing.ingest_span(
                {
                    "agent_id": "bot",
                    "attributes": {"veronica.cost_usd": 0.1},
                }
            )
        m = ing.get_agent_metrics("bot")
        assert m.call_count == 5


# ---------------------------------------------------------------------------
# Category 11: Extremely large metric values
# ---------------------------------------------------------------------------


class TestAdversarialExtremeValues:
    def test_extremely_large_cost(self) -> None:
        """1e308 cost ingested — must not crash."""
        ing = OTelMetricsIngester()
        ing.ingest_span(
            {
                "agent_id": "rich-bot",
                "attributes": {"veronica.cost_usd": 1e308},
            }
        )
        m = ing.get_agent_metrics("rich-bot")
        assert m.total_cost == pytest.approx(1e308)

    def test_inf_cost_is_rejected(self) -> None:
        """inf cost (not finite) — must be rejected (cost stays 0)."""
        ing = OTelMetricsIngester()
        ing.ingest_span(
            {
                "agent_id": "bot",
                "attributes": {"veronica.cost_usd": float("inf")},
            }
        )
        m = ing.get_agent_metrics("bot")
        assert m.total_cost == 0.0

    def test_very_large_token_count_accumulated(self) -> None:
        """Accumulate 10 spans with 1e9 tokens each."""
        ing = OTelMetricsIngester()
        for _ in range(10):
            ing.ingest_span(
                {
                    "agent_id": "token-bot",
                    "attributes": {"llm.token.count.total": 1_000_000_000},
                }
            )
        m = ing.get_agent_metrics("token-bot")
        assert m.total_tokens == 10 * 1_000_000_000

    def test_policy_handles_1e308_metric(self) -> None:
        """Policy with 1e308 threshold never triggers."""
        from veronica_core.policy.metrics_policy import (
            MetricRule,
            MetricsDrivenPolicy,
        )
        from veronica_core.runtime_policy import PolicyContext

        ing = OTelMetricsIngester()
        ing.ingest_span(
            {
                "agent_id": "bot",
                "attributes": {"veronica.cost_usd": 1e300},
            }
        )
        rules = [MetricRule("total_cost_usd", "gt", 1e308, "halt")]
        policy = MetricsDrivenPolicy(ingester=ing, rules=rules)
        d = policy.check(PolicyContext(entity_id="bot"))
        assert d.allowed is True  # 1e300 not > 1e308


# ---------------------------------------------------------------------------
# Category 12: TOCTOU — ingest between check() calls
# ---------------------------------------------------------------------------


class TestAdversarialTOCTOU:
    def test_ingest_between_two_checks_reflects_correctly(self) -> None:
        """State change between check() calls must be reflected immediately."""
        from veronica_core.policy.metrics_policy import (
            MetricRule,
            MetricsDrivenPolicy,
        )
        from veronica_core.runtime_policy import PolicyContext

        ing = OTelMetricsIngester()
        rules = [MetricRule("total_cost_usd", "gt", 1.0, "halt")]
        policy = MetricsDrivenPolicy(ingester=ing, rules=rules)

        # Check 1: no cost yet → allow
        d1 = policy.check(PolicyContext(entity_id="bot"))
        assert d1.allowed is True

        # Ingest while policy is "idle"
        ing.ingest_span(
            {
                "agent_id": "bot",
                "attributes": {"veronica.cost_usd": 2.0},
            }
        )

        # Check 2: cost=2.0 > 1.0 → halt
        d2 = policy.check(PolicyContext(entity_id="bot"))
        assert d2.allowed is False

    def test_concurrent_ingest_toctou_race(self) -> None:
        """10 threads ingesting while 10 threads checking — no state corruption."""
        from veronica_core.policy.metrics_policy import (
            MetricRule,
            MetricsDrivenPolicy,
        )
        from veronica_core.runtime_policy import PolicyContext

        ing = OTelMetricsIngester()
        rules = [MetricRule("error_rate", "gte", 0.5, "warn")]
        policy = MetricsDrivenPolicy(ingester=ing, rules=rules)
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


# ---------------------------------------------------------------------------
# Category 13: error_count snapshot correctness (regression for BUG-2/4)
# ---------------------------------------------------------------------------


class TestAdversarialErrorCountSnapshot:
    """Verify AgentMetrics._error_count is populated from real ingester snapshot.

    Regression tests for the bug where _AgentState.snapshot() did not copy
    error_count into AgentMetrics._error_count, causing MetricsDrivenPolicy
    error_count rules to always see 0.
    """

    def test_error_count_populated_in_snapshot(self) -> None:
        """snapshot() must expose _error_count equal to the actual error count."""
        ing = OTelMetricsIngester()
        for _ in range(7):
            ing.ingest_span({"agent_id": "bot", "status": "ERROR"})
        for _ in range(3):
            ing.ingest_span({"agent_id": "bot"})
        m = ing.get_agent_metrics("bot")
        assert m._error_count == 7, f"Expected 7 errors, got {m._error_count}"
        assert m.call_count == 10
        assert m.error_rate == pytest.approx(0.7)

    def test_error_count_rule_triggers_with_real_ingester(self) -> None:
        """error_count MetricRule must trigger when error count exceeds threshold."""
        ing = OTelMetricsIngester()
        for _ in range(10):
            ing.ingest_span({"agent_id": "bot", "status": "ERROR"})
        # 10 errors >= 5 → halt
        rule = MetricRule("error_count", "gte", 5, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ing)
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is False, (
            "error_count rule must trigger: 10 errors >= threshold 5"
        )

    def test_error_count_rule_does_not_trigger_below_threshold(self) -> None:
        """error_count MetricRule must not trigger when count is below threshold."""
        ing = OTelMetricsIngester()
        for _ in range(3):
            ing.ingest_span({"agent_id": "bot", "status": "ERROR"})
        rule = MetricRule("error_count", "gte", 5, "halt", agent_id="bot")
        policy = MetricsDrivenPolicy(rules=[rule], ingester=ing)
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is True, "3 errors should not trigger threshold=5"

    def test_error_count_snapshot_reset_to_zero(self) -> None:
        """After reset(), _error_count in snapshot must be 0."""
        ing = OTelMetricsIngester()
        for _ in range(5):
            ing.ingest_span({"agent_id": "bot", "status": "ERROR"})
        ing.reset("bot")
        m = ing.get_agent_metrics("bot")
        assert m._error_count == 0

    def test_error_count_concurrent_correctness(self) -> None:
        """Concurrent error span ingestion — final _error_count must be exact."""
        ing = OTelMetricsIngester()
        errors: list[Exception] = []

        def _ingest() -> None:
            try:
                for _ in range(20):
                    ing.ingest_span({"agent_id": "shared", "status": "ERROR"})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_ingest) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        m = ing.get_agent_metrics("shared")
        assert m._error_count == 200  # 10 threads × 20 errors
        assert m.error_rate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Category 14: _extract_metric broad exception safety (regression for BUG-3)
# ---------------------------------------------------------------------------


class TestAdversarialPropertyException:
    """Properties that raise non-standard exceptions must not crash the policy.

    Regression for the bug where _extract_metric only caught
    (TypeError, ValueError, AttributeError) — allowing ZeroDivisionError,
    RuntimeError, etc. to propagate out of policy.check().
    """

    def _policy_with_raising_property(self, exc_type: type, metric: str) -> Any:
        class RaisingMetrics:
            @property
            def total_cost(self) -> float:
                raise exc_type("injected error")

            @property
            def avg_latency_ms(self) -> float:
                raise exc_type("injected error")

            @property
            def error_rate(self) -> float:
                raise exc_type("injected error")

        class BrokenIngester:
            def get_agent_metrics(self, agent_id: str) -> RaisingMetrics:
                return RaisingMetrics()

        rule = MetricRule(metric, "gt", 0.0, "halt", agent_id="bot")
        return MetricsDrivenPolicy(rules=[rule], ingester=BrokenIngester())

    def test_zero_division_error_in_property_is_safe(self) -> None:
        """ZeroDivisionError raised by a metric property must not propagate."""
        policy = self._policy_with_raising_property(ZeroDivisionError, "total_cost_usd")
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is True, (
            "ZeroDivisionError in property must fail-safe to allow"
        )

    def test_runtime_error_in_property_is_safe(self) -> None:
        """RuntimeError raised by a metric property must not propagate."""
        policy = self._policy_with_raising_property(RuntimeError, "total_cost_usd")
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is True

    def test_os_error_in_property_is_safe(self) -> None:
        """OSError raised by a metric property must not propagate."""
        policy = self._policy_with_raising_property(OSError, "avg_latency_ms")
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is True

    def test_key_error_in_property_is_safe(self) -> None:
        """KeyError raised by a metric property must not propagate."""
        policy = self._policy_with_raising_property(KeyError, "error_rate")
        d = policy.check(_ctx(entity_id="bot"))
        assert d.allowed is True


# ---------------------------------------------------------------------------
# Category 15: Non-finite threshold regression (BUG-R2-1)
# ---------------------------------------------------------------------------


class TestAdversarialNonFiniteThreshold:
    """NaN / ±inf threshold must be rejected at MetricRule construction time.

    Security impact of NaN threshold (pre-fix): NaN comparisons always return
    False, so the rule would never trigger regardless of the observed value,
    silently bypassing all cost/error/latency limits.

    Impact of ±inf threshold: 'value < inf' is always True (constant halt),
    and 'value > -inf' is always True (constant halt) — both cause unexpected
    DoS-style policy behaviour that is almost certainly a configuration error.
    """

    def test_nan_threshold_rejected_at_construction(self) -> None:
        """MetricRule must raise ValueError for NaN threshold."""
        with pytest.raises(ValueError, match="finite"):
            MetricRule("total_cost_usd", "gt", float("nan"), "halt")

    def test_pos_inf_threshold_rejected_at_construction(self) -> None:
        """+inf threshold silently disables 'gt' rules and DoS-triggers 'lt' rules."""
        with pytest.raises(ValueError, match="finite"):
            MetricRule("error_rate", "lt", float("inf"), "halt")

    def test_neg_inf_threshold_rejected_at_construction(self) -> None:
        """-inf threshold makes 'gt' rules always trigger (DoS)."""
        with pytest.raises(ValueError, match="finite"):
            MetricRule("avg_latency_ms", "gt", float("-inf"), "degrade")

    def test_nan_threshold_via_yaml_like_dict_rejected(self) -> None:
        """Simulates YAML '.nan' deserialization reaching MetricRule constructor."""
        import math

        # YAML safe_load converts '.nan' to float('nan')
        params = {
            "rules": [
                {
                    "metric": "total_cost_usd",
                    "operator": "gt",
                    "threshold": math.nan,  # as if from YAML '.nan'
                    "action": "halt",
                }
            ]
        }
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(ValueError, match="finite"):
            factory(params)

    def test_inf_threshold_via_yaml_like_dict_rejected(self) -> None:
        """Simulates YAML '.inf' deserialization reaching MetricRule constructor."""
        import math

        params = {
            "rules": [
                {
                    "metric": "error_rate",
                    "operator": "lt",
                    "threshold": math.inf,  # as if from YAML '.inf'
                    "action": "halt",
                }
            ]
        }
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(ValueError, match="finite"):
            factory(params)


# ---------------------------------------------------------------------------
# Category 16: F.R.I.D.A.Y. R4 findings — factory validation, agent limit,
#              non-finite observed values
# ---------------------------------------------------------------------------


class TestAdversarialFridayR4:
    """Regression tests for F.R.I.D.A.Y. R4 audit findings."""

    def test_factory_rejects_non_list_rules(self) -> None:
        """rules must be a list; string/dict/int must raise TypeError."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(TypeError, match="list"):
            factory({"rules": "not a list"})
        with pytest.raises(TypeError, match="list"):
            factory({"rules": {"metric": "error_rate"}})

    def test_factory_rejects_non_dict_rule_entry(self) -> None:
        """Each rule entry must be a dict; string entries must raise TypeError."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(TypeError, match="dict"):
            factory({"rules": ["not a dict"]})

    def test_factory_coerces_numeric_agent_id_to_str(self) -> None:
        """Numeric agent_id from YAML should be coerced to string."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        policy = factory(
            {
                "rules": [
                    {
                        "metric": "total_cost_usd",
                        "operator": "gt",
                        "threshold": 1.0,
                        "action": "halt",
                        "agent_id": 42,
                    }
                ]
            }
        )
        assert policy.rules[0].agent_id == "42"

    def test_ingester_max_agents_limit(self) -> None:
        """New agents beyond max_agents must be silently dropped."""
        ing = OTelMetricsIngester(max_agents=5)
        for i in range(10):
            ing.ingest_span({"agent_id": f"agent-{i}"})
        all_agents = ing.get_all_agents()
        assert len(all_agents) == 5

    def test_ingester_max_agents_existing_agent_still_works(self) -> None:
        """Existing agents must still update after max_agents is reached."""
        ing = OTelMetricsIngester(max_agents=2)
        ing.ingest_span({"agent_id": "a"})
        ing.ingest_span({"agent_id": "b"})
        # Max reached; new agent dropped
        ing.ingest_span({"agent_id": "c"})
        # Existing agent still works
        ing.ingest_span({"agent_id": "a"})
        m = ing.get_agent_metrics("a")
        assert m.call_count == 2
        assert ing.get_agent_metrics("c").call_count == 0  # never created

    def test_extract_metric_filters_non_finite_observed(self) -> None:
        """NaN/inf observed values from metrics must be treated as None (skipped)."""
        from veronica_core.policy.metrics_policy import MetricsDrivenPolicy

        class InfMetrics:
            total_cost = float("inf")
            avg_latency_ms = float("nan")
            error_rate = float("-inf")

        assert (
            MetricsDrivenPolicy._extract_metric(InfMetrics(), "total_cost_usd") is None
        )
        assert (
            MetricsDrivenPolicy._extract_metric(InfMetrics(), "avg_latency_ms") is None
        )
        assert MetricsDrivenPolicy._extract_metric(InfMetrics(), "error_rate") is None


# ---------------------------------------------------------------------------
# Category 17: F.R.I.D.A.Y. R5 Audit Fixes
# ---------------------------------------------------------------------------


class TestR5AuditFixes:
    """Adversarial regression tests for R5 audit fixes.

    Attacker mindset: verify each fix holds under adversarial conditions.
    """

    # ------------------------------------------------------------------
    # Fix 1: cost_window maxlen — deque must not grow unbounded
    # ------------------------------------------------------------------

    def test_r5_cost_window_maxlen_not_exceeded(self) -> None:
        """Ingest 200K+ cost spans; cost_window deque must stay within maxlen."""
        max_size = 1000
        ing = OTelMetricsIngester(max_cost_window_size=max_size)
        # Each span has a cost so it appends to cost_window
        for i in range(200_000):
            ing.ingest_span(
                {
                    "agent_id": "bot",
                    "attributes": {"veronica.cost_usd": 0.001},
                }
            )
        # Access internal state to verify bound
        with ing._global_lock:
            state = ing._agents.get("bot")
        assert state is not None
        with state.lock:
            assert len(state.cost_window) <= max_size

    def test_r5_cost_window_default_maxlen_bounded(self) -> None:
        """Default max_cost_window_size must cap the deque (not unlimited)."""
        ing = OTelMetricsIngester()
        default_max = ing._max_cost_window_size
        assert default_max > 0, "default maxlen must be positive"
        # Ingest default_max + 100 spans with cost
        for _ in range(default_max + 100):
            ing.ingest_span(
                {
                    "agent_id": "agent",
                    "attributes": {"veronica.cost_usd": 0.001},
                }
            )
        with ing._global_lock:
            state = ing._agents.get("agent")
        assert state is not None
        with state.lock:
            assert len(state.cost_window) <= default_max

    # ------------------------------------------------------------------
    # Fix 2: reset() lock ordering — no deadlock under concurrent access
    # ------------------------------------------------------------------

    def test_r5_reset_lock_ordering_no_deadlock(self) -> None:
        """Concurrent reset() + ingest_span() on 10 threads must not deadlock."""
        ing = OTelMetricsIngester()
        # Seed some agents
        for i in range(5):
            ing.ingest_span({"agent_id": f"agent-{i}"})

        errors: list[Exception] = []
        deadline = time.monotonic() + 5.0  # 5-second timeout

        def ingest_loop() -> None:
            try:
                while time.monotonic() < deadline:
                    for i in range(5):
                        ing.ingest_span(
                            {
                                "agent_id": f"agent-{i}",
                                "attributes": {"veronica.cost_usd": 0.001},
                            }
                        )
            except Exception as exc:
                errors.append(exc)

        def reset_loop() -> None:
            try:
                while time.monotonic() < deadline:
                    ing.reset()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=ingest_loop) for _ in range(8)]
        threads += [threading.Thread(target=reset_loop) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=6.0)

        # All threads must have finished (no deadlock)
        for t in threads:
            assert not t.is_alive(), "thread still alive — possible deadlock"
        assert not errors, f"exceptions in threads: {errors}"

    def test_r5_reset_single_agent_concurrent_ingest(self) -> None:
        """reset(agent_id) concurrent with ingest_span must not deadlock."""
        ing = OTelMetricsIngester()
        ing.ingest_span({"agent_id": "target"})

        stop = threading.Event()
        errors: list[Exception] = []

        def ingest() -> None:
            try:
                while not stop.is_set():
                    ing.ingest_span(
                        {
                            "agent_id": "target",
                            "attributes": {"veronica.cost_usd": 0.001},
                        }
                    )
            except Exception as exc:
                errors.append(exc)

        def resetter() -> None:
            try:
                for _ in range(500):
                    ing.reset("target")
            except Exception as exc:
                errors.append(exc)

        t_ingest = threading.Thread(target=ingest, daemon=True)
        t_reset = threading.Thread(target=resetter)
        t_ingest.start()
        t_reset.start()
        t_reset.join(timeout=5.0)
        stop.set()
        t_ingest.join(timeout=2.0)

        assert not t_reset.is_alive(), "reset thread deadlocked"
        assert not errors, f"exceptions: {errors}"

    # ------------------------------------------------------------------
    # Fix 3: ingest_span logging — malformed span triggers logger.debug
    # ------------------------------------------------------------------

    def test_r5_ingest_span_logging_on_internal_exception(self, caplog: Any) -> None:
        """When _ingest_span_internal raises, logger.debug must be called."""
        import logging
        from unittest.mock import patch

        ing = OTelMetricsIngester()

        def _explode(span: dict) -> None:
            raise RuntimeError("injected failure")

        with patch.object(ing, "_ingest_span_internal", side_effect=_explode):
            with caplog.at_level(
                logging.DEBUG, logger="veronica_core.otel_feedback.ingester"
            ):
                ing.ingest_span({"name": "bad-span", "agent_id": "x"})

        # Must have logged a debug message (never re-raises)
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert debug_records, "expected logger.debug call on ingest_span failure"

    def test_r5_ingest_span_never_raises_on_malformed(self) -> None:
        """ingest_span must silently absorb all exceptions — never propagate."""
        ing = OTelMetricsIngester()
        # Various malformed inputs
        malformed_inputs = [
            None,
            42,
            "string",
            [],
            {"attributes": "not-a-dict"},
            {"start_time": "NaN", "end_time": "NaN"},
            {"attributes": {"llm.token.count.total": float("nan")}},
        ]
        for bad in malformed_inputs:
            try:
                ing.ingest_span(bad)  # type: ignore[arg-type]
            except Exception as exc:
                pytest.fail(f"ingest_span raised for {bad!r}: {exc}")

    # ------------------------------------------------------------------
    # Fix 4: _get_metrics fail warning — fail-open with logger.warning
    # ------------------------------------------------------------------

    def test_r5_get_metrics_fail_open_returns_none(self) -> None:
        """_get_metrics must return None (not raise) when ingester explodes."""

        class BrokenIngester:
            def get_agent_metrics(self, agent_id: str) -> Any:
                raise RuntimeError("ingester down")

        result = MetricsDrivenPolicy._get_metrics(BrokenIngester(), "agent-x")
        assert result is None

    def test_r5_get_metrics_fail_open_emits_warning(self, caplog: Any) -> None:
        """_get_metrics failure must emit logger.warning."""
        import logging

        class BrokenIngester:
            def get_agent_metrics(self, agent_id: str) -> Any:
                raise RuntimeError("simulated ingester failure")

        with caplog.at_level(
            logging.WARNING, logger="veronica_core.policy.metrics_policy"
        ):
            MetricsDrivenPolicy._get_metrics(BrokenIngester(), "agent-y")

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "expected logger.warning on _get_metrics failure"
        assert "agent-y" in warning_records[0].message or "agent-y" in str(
            warning_records[0].args
        )

    def test_r5_get_metrics_fail_open_policy_allows(self) -> None:
        """check() must allow (fail-open) when ingester raises on get_agent_metrics."""

        class BrokenIngester:
            def get_agent_metrics(self, agent_id: str) -> Any:
                raise RuntimeError("ingester down")

        policy = MetricsDrivenPolicy(
            rules=[MetricRule("total_cost_usd", "gt", 0.0, "halt")],
            ingester=BrokenIngester(),
            agent_id="agent-z",
        )
        decision = policy.check(_ctx("agent-z"))
        assert decision.allowed, "policy must fail-open when ingester raises"

    # ------------------------------------------------------------------
    # Fix 5: error_count public property on AgentMetrics
    # ------------------------------------------------------------------

    def test_r5_error_count_property_returns_internal_value(self) -> None:
        """AgentMetrics.error_count property must return _error_count field."""
        from veronica_core.otel_feedback.ingester import AgentMetrics

        m = AgentMetrics(_error_count=5)
        assert m.error_count == 5

    def test_r5_error_count_property_zero_by_default(self) -> None:
        """AgentMetrics.error_count must default to 0."""
        from veronica_core.otel_feedback.ingester import AgentMetrics

        m = AgentMetrics()
        assert m.error_count == 0

    def test_r5_error_count_tracks_error_spans(self) -> None:
        """error_count on snapshot must reflect actual error spans ingested."""
        ing = OTelMetricsIngester()
        # 3 error spans, 2 normal
        for _ in range(3):
            ing.ingest_span({"agent_id": "bot", "status": "ERROR"})
        for _ in range(2):
            ing.ingest_span({"agent_id": "bot"})
        m = ing.get_agent_metrics("bot")
        assert m.error_count == 3
        assert m.call_count == 5

    # ------------------------------------------------------------------
    # Fix 6-8: _make_metric_rule null/empty validation
    # ------------------------------------------------------------------

    def test_r5_metric_null_raises_type_error(self) -> None:
        """metric=None in rule dict must raise TypeError."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(TypeError, match="metric"):
            factory(
                {
                    "rules": [
                        {
                            "metric": None,
                            "operator": "gt",
                            "action": "halt",
                            "threshold": 1.0,
                        }
                    ]
                }
            )

    def test_r5_metric_empty_string_raises_type_error(self) -> None:
        """metric='' in rule dict must raise TypeError."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(TypeError, match="metric"):
            factory(
                {
                    "rules": [
                        {
                            "metric": "",
                            "operator": "gt",
                            "action": "halt",
                            "threshold": 1.0,
                        }
                    ]
                }
            )

    def test_r5_metric_zero_raises_type_error(self) -> None:
        """metric=0 (falsy non-string) in rule dict must raise TypeError."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(TypeError, match="metric"):
            factory(
                {
                    "rules": [
                        {
                            "metric": 0,
                            "operator": "gt",
                            "action": "halt",
                            "threshold": 1.0,
                        }
                    ]
                }
            )

    def test_r5_operator_null_raises_type_error(self) -> None:
        """operator=None in rule dict must raise TypeError."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(TypeError, match="operator"):
            factory(
                {
                    "rules": [
                        {
                            "metric": "total_cost_usd",
                            "operator": None,
                            "action": "halt",
                            "threshold": 1.0,
                        }
                    ]
                }
            )

    def test_r5_operator_empty_string_raises_type_error(self) -> None:
        """operator='' in rule dict must raise TypeError."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(TypeError, match="operator"):
            factory(
                {
                    "rules": [
                        {
                            "metric": "total_cost_usd",
                            "operator": "",
                            "action": "halt",
                            "threshold": 1.0,
                        }
                    ]
                }
            )

    def test_r5_operator_zero_raises_type_error(self) -> None:
        """operator=0 (falsy non-string) in rule dict must raise TypeError."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(TypeError, match="operator"):
            factory(
                {
                    "rules": [
                        {
                            "metric": "total_cost_usd",
                            "operator": 0,
                            "action": "halt",
                            "threshold": 1.0,
                        }
                    ]
                }
            )

    def test_r5_action_null_raises_type_error(self) -> None:
        """action=None in rule dict must raise TypeError."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(TypeError, match="action"):
            factory(
                {
                    "rules": [
                        {
                            "metric": "total_cost_usd",
                            "operator": "gt",
                            "action": None,
                            "threshold": 1.0,
                        }
                    ]
                }
            )

    def test_r5_action_empty_string_raises_type_error(self) -> None:
        """action='' in rule dict must raise TypeError."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(TypeError, match="action"):
            factory(
                {
                    "rules": [
                        {
                            "metric": "total_cost_usd",
                            "operator": "gt",
                            "action": "",
                            "threshold": 1.0,
                        }
                    ]
                }
            )

    def test_r5_action_zero_raises_type_error(self) -> None:
        """action=0 (falsy non-string) in rule dict must raise TypeError."""
        from veronica_core.policy.registry import PolicyRegistry

        registry = PolicyRegistry()
        factory = registry.get_rule_type("metric_rule")
        with pytest.raises(TypeError, match="action"):
            factory(
                {
                    "rules": [
                        {
                            "metric": "total_cost_usd",
                            "operator": "gt",
                            "action": 0,
                            "threshold": 1.0,
                        }
                    ]
                }
            )
