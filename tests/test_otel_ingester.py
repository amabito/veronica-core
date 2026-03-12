"""Unit tests for OTelMetricsIngester and AgentMetrics.

Categories:
  1. AgentMetrics defaults
  2. ingest_span() with AG2-format spans
  3. ingest_span() with generic OTel spans
  4. Aggregation correctness (avg_latency, error_rate, total_cost, total_tokens)
  5. get_all_agents()
  6. reset()
  7. Sliding window expiry
  8. Thread-safety
  9. Edge cases (empty, missing attrs, unknown span types, malformed values)
"""

from __future__ import annotations

import threading
import time

import pytest

from veronica_core.otel_feedback import AgentMetrics, OTelMetricsIngester
from veronica_core.otel_feedback.ingester import (
    _extract_cost,
    _extract_duration_ms,
    _extract_tokens,
    _is_error_span,
    _resolve_agent_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_span(
    agent_id: str = "assistant",
    cost: float = 0.01,
    tokens: int = 500,
    duration_ms: float = 200.0,
    is_error: bool = False,
) -> dict:
    start = 1700000000.0
    return {
        "name": "llm_call",
        "span_type": "llm",
        "agent_id": agent_id,
        "start_time": start,
        "end_time": start + duration_ms / 1000.0,
        "status": "ERROR" if is_error else "OK",
        "attributes": {
            "veronica.cost_usd": cost,
            "llm.token.count.total": tokens,
        },
    }


def _make_tool_span(agent_id: str = "planner", duration_ms: float = 50.0) -> dict:
    start = 1700000100.0
    return {
        "name": "tool_call",
        "span_type": "tool",
        "agent_id": agent_id,
        "start_time": start,
        "end_time": start + duration_ms / 1000.0,
        "attributes": {},
    }


def _make_generic_span(
    agent_id: str = "generic_agent",
    cost: float = 0.005,
    total_tokens: int = 300,
    duration_ms: float = 100.0,
) -> dict:
    """Generic OTel span without span_type (no AG2 classification)."""
    start = 1700000200.0
    return {
        "name": "llm_call",
        "agent_id": agent_id,
        "start_time": start,
        "end_time": start + duration_ms / 1000.0,
        "attributes": {
            "llm.cost": cost,
            "llm.token.count.total": total_tokens,
        },
    }


# ---------------------------------------------------------------------------
# 1. AgentMetrics defaults
# ---------------------------------------------------------------------------


class TestAgentMetricsDefaults:
    def test_all_numeric_fields_are_zero_by_default(self):
        m = AgentMetrics()
        assert m.total_tokens == 0
        assert m.total_cost == 0.0
        assert m.avg_latency_ms == 0.0
        assert m.error_rate == 0.0
        assert m.last_active == 0.0
        assert m.call_count == 0

    def test_can_be_constructed_with_values(self):
        m = AgentMetrics(
            total_tokens=1000,
            total_cost=0.05,
            avg_latency_ms=250.0,
            error_rate=0.1,
            last_active=123.456,
            call_count=10,
        )
        assert m.total_tokens == 1000
        assert m.total_cost == 0.05
        assert m.avg_latency_ms == 250.0
        assert m.error_rate == 0.1
        assert m.call_count == 10

    def test_is_dataclass(self):
        m = AgentMetrics()
        assert hasattr(m, "__dataclass_fields__")


# ---------------------------------------------------------------------------
# 2. ingest_span() -- AG2 span types
# ---------------------------------------------------------------------------


class TestIngestSpanAG2Formats:
    def test_llm_span_updates_cost_and_tokens(self):
        ingester = OTelMetricsIngester()
        span = _make_llm_span(agent_id="bot", cost=0.02, tokens=800)
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("bot")
        assert m.total_cost == pytest.approx(0.02)
        assert m.total_tokens == 800
        assert m.call_count == 1

    def test_tool_span_updates_call_count(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_tool_span(agent_id="planner"))
        m = ingester.get_agent_metrics("planner")
        assert m.call_count == 1

    @pytest.mark.parametrize("span_type", ["conversation", "agent", "code_execution"])
    def test_all_ag2_span_types_are_ingested(self, span_type: str):
        ingester = OTelMetricsIngester()
        span = {
            "name": span_type,
            "span_type": span_type,
            "agent_id": "agent_x",
            "start_time": 1700000000.0,
            "end_time": 1700000001.0,
            "attributes": {"llm.cost": 0.001},
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("agent_x")
        assert m.call_count == 1

    def test_unknown_span_type_is_skipped(self):
        ingester = OTelMetricsIngester()
        span = {
            "name": "custom_type",
            "span_type": "my_custom_span",
            "agent_id": "agent_y",
            "attributes": {"llm.cost": 0.01},
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("agent_y")
        assert m.call_count == 0  # skipped

    def test_error_status_sets_error_rate(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="err_bot", is_error=True))
        ingester.ingest_span(_make_llm_span(agent_id="err_bot", is_error=False))
        m = ingester.get_agent_metrics("err_bot")
        assert m.error_rate == pytest.approx(0.5)
        assert m.call_count == 2

    def test_duration_computed_from_start_end_times(self):
        ingester = OTelMetricsIngester()
        span = _make_llm_span(agent_id="timing_bot", duration_ms=300.0)
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("timing_bot")
        assert m.avg_latency_ms == pytest.approx(300.0, abs=1.0)


# ---------------------------------------------------------------------------
# 3. ingest_span() -- generic OTel spans
# ---------------------------------------------------------------------------


class TestIngestSpanGenericOTel:
    def test_generic_span_without_span_type_is_ingested(self):
        ingester = OTelMetricsIngester()
        span = _make_generic_span(agent_id="gen_bot", cost=0.005, total_tokens=300)
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("gen_bot")
        assert m.total_cost == pytest.approx(0.005)
        assert m.total_tokens == 300

    def test_gen_ai_usage_tokens_attribute(self):
        ingester = OTelMetricsIngester()
        span = {
            "name": "llm",
            "agent_id": "gen2",
            "attributes": {
                "gen_ai.usage.total_tokens": 700,
                "veronica.cost_usd": 0.007,
            },
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("gen2")
        assert m.total_tokens == 700

    def test_llm_input_output_tokens_summed(self):
        ingester = OTelMetricsIngester()
        span = {
            "name": "llm",
            "agent_id": "sum_agent",
            "attributes": {
                "gen_ai.usage.prompt_tokens": 400,
                "gen_ai.usage.completion_tokens": 200,
            },
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("sum_agent")
        assert m.total_tokens == 600

    def test_veronica_cost_usd_attribute(self):
        ingester = OTelMetricsIngester()
        span = {
            "name": "veronica_wrap",
            "agent_id": "v_bot",
            "attributes": {"veronica.cost_usd": 0.123},
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("v_bot")
        assert m.total_cost == pytest.approx(0.123)


# ---------------------------------------------------------------------------
# 4. Aggregation correctness
# ---------------------------------------------------------------------------


class TestAggregation:
    def test_multiple_spans_accumulate_cost(self):
        ingester = OTelMetricsIngester()
        for _ in range(5):
            ingester.ingest_span(_make_llm_span(agent_id="acc", cost=0.01))
        m = ingester.get_agent_metrics("acc")
        assert m.total_cost == pytest.approx(0.05)
        assert m.call_count == 5

    def test_multiple_spans_accumulate_tokens(self):
        ingester = OTelMetricsIngester()
        for i in range(3):
            ingester.ingest_span(_make_llm_span(agent_id="tok", tokens=100 * (i + 1)))
        m = ingester.get_agent_metrics("tok")
        assert m.total_tokens == 600  # 100+200+300

    def test_avg_latency_computed_correctly(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="lat", duration_ms=100.0))
        ingester.ingest_span(_make_llm_span(agent_id="lat", duration_ms=300.0))
        m = ingester.get_agent_metrics("lat")
        assert m.avg_latency_ms == pytest.approx(200.0, abs=1.0)

    def test_error_rate_zero_when_no_errors(self):
        ingester = OTelMetricsIngester()
        for _ in range(4):
            ingester.ingest_span(_make_llm_span(agent_id="clean"))
        m = ingester.get_agent_metrics("clean")
        assert m.error_rate == 0.0

    def test_error_rate_one_when_all_errors(self):
        ingester = OTelMetricsIngester()
        for _ in range(3):
            ingester.ingest_span(_make_llm_span(agent_id="all_err", is_error=True))
        m = ingester.get_agent_metrics("all_err")
        assert m.error_rate == pytest.approx(1.0)

    def test_last_active_is_monotonically_updated(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="ts_bot"))
        m1 = ingester.get_agent_metrics("ts_bot")
        time.sleep(0.01)
        ingester.ingest_span(_make_llm_span(agent_id="ts_bot"))
        m2 = ingester.get_agent_metrics("ts_bot")
        assert m2.last_active >= m1.last_active

    def test_separate_agents_tracked_independently(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="alice", cost=0.01, tokens=100))
        ingester.ingest_span(_make_llm_span(agent_id="bob", cost=0.05, tokens=500))
        alice = ingester.get_agent_metrics("alice")
        bob = ingester.get_agent_metrics("bob")
        assert alice.total_cost == pytest.approx(0.01)
        assert bob.total_cost == pytest.approx(0.05)
        assert alice.total_tokens == 100
        assert bob.total_tokens == 500


# ---------------------------------------------------------------------------
# 5. get_all_agents()
# ---------------------------------------------------------------------------


class TestGetAllAgents:
    def test_returns_empty_dict_when_no_spans_ingested(self):
        ingester = OTelMetricsIngester()
        assert ingester.get_all_agents() == {}

    def test_returns_all_tracked_agent_ids(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="a1"))
        ingester.ingest_span(_make_llm_span(agent_id="a2"))
        ingester.ingest_span(_make_llm_span(agent_id="a3"))
        all_agents = ingester.get_all_agents()
        assert set(all_agents.keys()) == {"a1", "a2", "a3"}

    def test_values_are_agent_metrics_instances(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="m1"))
        all_agents = ingester.get_all_agents()
        assert isinstance(all_agents["m1"], AgentMetrics)

    def test_values_match_individual_get(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="match", cost=0.07))
        all_agents = ingester.get_all_agents()
        individual = ingester.get_agent_metrics("match")
        assert all_agents["match"].total_cost == pytest.approx(individual.total_cost)


# ---------------------------------------------------------------------------
# 6. reset()
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_specific_agent_clears_metrics(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="reset_me", cost=0.5))
        ingester.reset("reset_me")
        m = ingester.get_agent_metrics("reset_me")
        assert m.call_count == 0
        assert m.total_cost == 0.0

    def test_reset_specific_agent_does_not_affect_others(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="keep", cost=0.1))
        ingester.ingest_span(_make_llm_span(agent_id="wipe", cost=0.2))
        ingester.reset("wipe")
        m_keep = ingester.get_agent_metrics("keep")
        m_wipe = ingester.get_agent_metrics("wipe")
        assert m_keep.total_cost == pytest.approx(0.1)
        assert m_wipe.call_count == 0

    def test_reset_none_clears_all_agents(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="x"))
        ingester.ingest_span(_make_llm_span(agent_id="y"))
        ingester.reset()
        assert ingester.get_agent_metrics("x").call_count == 0
        assert ingester.get_agent_metrics("y").call_count == 0

    def test_reset_unknown_agent_is_noop(self):
        ingester = OTelMetricsIngester()
        ingester.reset("nonexistent")  # Should not raise

    def test_reset_preserves_agent_registry_entries(self):
        """reset() zeroes counters but does NOT remove agents from get_all_agents().

        This is documented behaviour: agent entries are kept in memory so that
        subsequent ingest_span() calls continue to accumulate correctly without
        re-creating state objects.  Create a new OTelMetricsIngester to fully
        reclaim memory.
        """
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="a"))
        ingester.ingest_span(_make_llm_span(agent_id="b"))
        ingester.reset()
        # Entries still present, but with zero metrics
        all_agents = ingester.get_all_agents()
        assert set(all_agents.keys()) == {"a", "b"}
        for m in all_agents.values():
            assert m.call_count == 0
            assert m.total_cost == 0.0

    def test_get_after_reset_returns_zeros(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(_make_llm_span(agent_id="z", cost=99.0))
        ingester.reset("z")
        m = ingester.get_agent_metrics("z")
        assert m.total_tokens == 0
        assert m.total_cost == 0.0
        assert m.avg_latency_ms == 0.0
        assert m.error_rate == 0.0


# ---------------------------------------------------------------------------
# 7. Sliding window
# ---------------------------------------------------------------------------


class TestSlidingWindow:
    def test_short_window_accepts_initial_entries(self):
        # With a very short window, entries added now should still be present
        ingester = OTelMetricsIngester(window_sec=60.0)
        ingester.ingest_span(_make_llm_span(agent_id="win", cost=0.05))
        m = ingester.get_agent_metrics("win")
        assert m.total_cost == pytest.approx(0.05)

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            OTelMetricsIngester(window_sec=0.0)
        with pytest.raises(ValueError):
            OTelMetricsIngester(window_sec=-1.0)

    def test_window_prunes_old_entries_after_sleep(self):
        """Cost window should drop entries older than window_sec.

        We use a 0.05s window. After sleeping 0.1s, the first entry should
        be pruned when the second is ingested.
        """
        ingester = OTelMetricsIngester(window_sec=0.05)
        ingester.ingest_span(_make_llm_span(agent_id="prune", cost=10.0))
        time.sleep(0.1)
        ingester.ingest_span(_make_llm_span(agent_id="prune", cost=1.0))
        # cost_window should now contain only the second entry
        with ingester._global_lock:
            state = ingester._agents["prune"]
        with state.lock:
            assert len(state.cost_window) == 1
            assert state.cost_window[0][1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 8. Thread-safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_ingest_from_multiple_threads(self):
        """100 threads each ingest 10 spans for the same agent -- no crash, count consistent."""
        ingester = OTelMetricsIngester()
        errors: list[Exception] = []

        def worker():
            try:
                for _ in range(10):
                    ingester.ingest_span(_make_llm_span(agent_id="shared", cost=0.001))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        m = ingester.get_agent_metrics("shared")
        assert m.call_count == 1000
        assert m.total_cost == pytest.approx(1.0, abs=1e-6)

    def test_concurrent_ingest_different_agents(self):
        """50 threads each ingesting to a unique agent_id -- no data corruption."""
        ingester = OTelMetricsIngester()
        errors: list[Exception] = []

        def worker(agent_id: str):
            try:
                for _ in range(5):
                    ingester.ingest_span(
                        _make_llm_span(agent_id=agent_id, cost=0.01, tokens=100)
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"agent_{i}",)) for i in range(50)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        all_agents = ingester.get_all_agents()
        assert len(all_agents) == 50
        for agent_id, m in all_agents.items():
            assert m.call_count == 5, (
                f"{agent_id}: expected 5 calls, got {m.call_count}"
            )
            assert m.total_cost == pytest.approx(0.05, abs=1e-9)

    def test_concurrent_reset_and_ingest(self):
        """Reset during concurrent ingestion must not raise."""
        ingester = OTelMetricsIngester()
        stop = threading.Event()
        errors: list[Exception] = []

        def ingest_worker():
            while not stop.is_set():
                try:
                    ingester.ingest_span(_make_llm_span(agent_id="race"))
                except Exception as exc:
                    errors.append(exc)

        def reset_worker():
            for _ in range(50):
                try:
                    ingester.reset("race")
                    time.sleep(0.001)
                except Exception as exc:
                    errors.append(exc)

        ingest_threads = [threading.Thread(target=ingest_worker) for _ in range(5)]
        reset_thread = threading.Thread(target=reset_worker)
        for t in ingest_threads:
            t.start()
        reset_thread.start()
        reset_thread.join()
        stop.set()
        for t in ingest_threads:
            t.join()

        assert errors == []


# ---------------------------------------------------------------------------
# 9. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_span_dict_does_not_raise(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span({})  # should silently succeed

    def test_none_span_does_not_raise(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span(None)  # type: ignore[arg-type]

    def test_non_dict_span_does_not_raise(self):
        ingester = OTelMetricsIngester()
        ingester.ingest_span("not a dict")  # type: ignore[arg-type]
        ingester.ingest_span(42)  # type: ignore[arg-type]

    def test_missing_agent_id_falls_back_to_span_name(self):
        ingester = OTelMetricsIngester()
        span = {
            "name": "my_span",
            "start_time": 1700000000.0,
            "end_time": 1700000001.0,
            "attributes": {"veronica.cost_usd": 0.01},
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("my_span")
        assert m.call_count == 1

    def test_negative_cost_is_ignored(self):
        ingester = OTelMetricsIngester()
        span = {
            "name": "bad_cost",
            "agent_id": "neg",
            "attributes": {"veronica.cost_usd": -5.0},
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("neg")
        assert m.total_cost == 0.0

    def test_nan_cost_is_ignored(self):
        ingester = OTelMetricsIngester()
        import math

        span = {
            "name": "nan_cost",
            "agent_id": "nan_agent",
            "attributes": {"veronica.cost_usd": math.nan},
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("nan_agent")
        assert m.total_cost == 0.0

    def test_missing_timing_does_not_affect_call_count(self):
        ingester = OTelMetricsIngester()
        span = {
            "name": "no_time",
            "agent_id": "notimer",
            "attributes": {"veronica.cost_usd": 0.01},
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("notimer")
        assert m.call_count == 1
        assert m.avg_latency_ms == 0.0  # no timing data → stays 0

    def test_negative_end_before_start_ignored_gracefully(self):
        ingester = OTelMetricsIngester()
        span = {
            "name": "badtime",
            "agent_id": "badtime_agent",
            "start_time": 1700000010.0,
            "end_time": 1700000005.0,  # end < start
            "attributes": {},
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("badtime_agent")
        assert m.call_count == 1
        assert m.avg_latency_ms == 0.0

    def test_get_agent_metrics_unknown_returns_zeroed(self):
        ingester = OTelMetricsIngester()
        m = ingester.get_agent_metrics("never_seen")
        assert m.call_count == 0
        assert m.total_cost == 0.0

    def test_veronica_halt_decision_counts_as_error(self):
        ingester = OTelMetricsIngester()
        span = {
            "name": "halt_span",
            "agent_id": "halt_bot",
            "attributes": {"veronica.decision": "HALT"},
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("halt_bot")
        assert m.error_rate == pytest.approx(1.0)

    def test_exception_attribute_counts_as_error(self):
        ingester = OTelMetricsIngester()
        span = {
            "name": "exc_span",
            "agent_id": "exc_bot",
            "attributes": {"exception.type": "RuntimeError"},
        }
        ingester.ingest_span(span)
        m = ingester.get_agent_metrics("exc_bot")
        assert m.error_rate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Private helper unit tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_extract_duration_ms_from_start_end(self):
        span = {"start_time": 1700000000.0, "end_time": 1700000001.5}
        assert _extract_duration_ms(span) == pytest.approx(1500.0)

    def test_extract_duration_ms_from_direct_field(self):
        span = {"duration_ms": 750.0}
        assert _extract_duration_ms(span) == pytest.approx(750.0)

    def test_extract_duration_ms_nanoseconds(self):
        # 1e9 ns = 1 second
        span = {"start_time": int(1.7e18), "end_time": int(1.7e18 + 1e9)}
        dur = _extract_duration_ms(span)
        assert dur == pytest.approx(1000.0, abs=1.0)

    def test_extract_duration_ms_missing_returns_none(self):
        assert _extract_duration_ms({}) is None
        assert _extract_duration_ms({"start_time": 100.0}) is None

    def test_extract_tokens_total_key(self):
        assert _extract_tokens({"llm.token.count.total": 400}) == 400

    def test_extract_tokens_sum_input_output(self):
        attrs = {
            "llm.token.count.prompt": 300,
            "llm.token.count.completion": 150,
        }
        assert _extract_tokens(attrs) == 450

    def test_extract_cost_veronica_priority(self):
        attrs = {"veronica.cost_usd": 0.09, "llm.cost": 0.05}
        assert _extract_cost(attrs) == pytest.approx(0.09)

    def test_extract_cost_fallback_llm_cost(self):
        attrs = {"llm.cost": 0.03}
        assert _extract_cost(attrs) == pytest.approx(0.03)

    def test_extract_cost_missing_returns_zero(self):
        assert _extract_cost({}) == 0.0

    def test_is_error_span_status_error(self):
        assert _is_error_span({"status": "ERROR"}, {}) is True

    def test_is_error_span_ok_not_error(self):
        assert _is_error_span({"status": "OK"}, {}) is False

    def test_resolve_agent_id_from_veronica_attr(self):
        span = {"name": "span_name"}
        attrs = {"veronica.agent_id": "veronica_bot"}
        assert _resolve_agent_id(span, attrs) == "veronica_bot"

    def test_resolve_agent_id_falls_back_to_name(self):
        span = {"name": "fallback_span"}
        assert _resolve_agent_id(span, {}) == "fallback_span"

    def test_resolve_agent_id_unknown_when_no_name(self):
        assert _resolve_agent_id({}, {}) == "unknown"
