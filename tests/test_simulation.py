"""Tests for veronica_core.simulation -- Policy Simulation (Phase F).

Covers ExecutionLogEntry, ExecutionLog, PolicySimulator, SimulationReport,
and OTel span conversion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.types import Decision, ToolCallContext
from veronica_core.simulation import (
    ExecutionLog,
    ExecutionLogEntry,
    PolicySimulator,
    SimulationEvent,
    SimulationReport,
)


# ---------------------------------------------------------------------------
# ExecutionLogEntry tests
# ---------------------------------------------------------------------------


class TestExecutionLogEntry:
    def test_valid_llm_call(self) -> None:
        entry = ExecutionLogEntry(
            timestamp=1700000000.0,
            agent_id="bot",
            action="llm_call",
            cost_usd=0.05,
            tokens=500,
        )
        assert entry.action == "llm_call"
        assert entry.cost_usd == 0.05
        assert entry.tokens == 500

    def test_valid_tool_call(self) -> None:
        entry = ExecutionLogEntry(
            timestamp=1700000001.0,
            agent_id="bot",
            action="tool_call",
        )
        assert entry.action == "tool_call"

    def test_valid_reply(self) -> None:
        entry = ExecutionLogEntry(
            timestamp=1700000002.0,
            agent_id="bot",
            action="reply",
        )
        assert entry.action == "reply"

    def test_invalid_action_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            ExecutionLogEntry(timestamp=0.0, agent_id="x", action="unknown")

    def test_negative_cost_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            ExecutionLogEntry(timestamp=0.0, agent_id="x", action="llm_call", cost_usd=-1.0)

    def test_negative_tokens_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            ExecutionLogEntry(timestamp=0.0, agent_id="x", action="llm_call", tokens=-10)

    def test_frozen(self) -> None:
        entry = ExecutionLogEntry(timestamp=0.0, agent_id="x", action="llm_call")
        with pytest.raises(AttributeError):
            entry.action = "tool_call"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ExecutionLog tests
# ---------------------------------------------------------------------------


class TestExecutionLog:
    def test_from_file_object_format(self, tmp_path: Path) -> None:
        data = {
            "entries": [
                {"timestamp": 2.0, "agent_id": "b", "action": "llm_call", "cost_usd": 0.1},
                {"timestamp": 1.0, "agent_id": "a", "action": "tool_call"},
            ]
        }
        p = tmp_path / "log.json"
        p.write_text(json.dumps(data))
        log = ExecutionLog.from_file(p)
        assert len(log) == 2
        # Sorted by timestamp
        assert log.entries[0].agent_id == "a"
        assert log.entries[1].agent_id == "b"

    def test_from_file_array_format(self, tmp_path: Path) -> None:
        data = [
            {"timestamp": 1.0, "agent_id": "a", "action": "llm_call"},
        ]
        p = tmp_path / "log.json"
        p.write_text(json.dumps(data))
        log = ExecutionLog.from_file(p)
        assert len(log) == 1

    def test_from_string(self) -> None:
        content = json.dumps([
            {"timestamp": 1.0, "agent_id": "a", "action": "reply"},
        ])
        log = ExecutionLog.from_string(content)
        assert len(log) == 1

    def test_from_otel_export(self) -> None:
        spans = [
            {
                "name": "llm.generate",
                "start_time": 1700000000000000000,  # nanoseconds
                "end_time": 1700000000500000000,
                "attributes": {
                    "gen_ai.usage.prompt_tokens": 100,
                    "gen_ai.usage.completion_tokens": 50,
                    "gen_ai.request.model": "gpt-4o",
                },
                "status": {"status_code": "OK"},
            },
        ]
        log = ExecutionLog.from_otel_export(spans)
        assert len(log) == 1
        entry = log.entries[0]
        assert entry.model == "gpt-4o"
        assert entry.tokens == 150
        assert entry.latency_ms == 500.0
        assert entry.success is True

    def test_from_otel_tool_span(self) -> None:
        spans = [
            {
                "name": "tool.execute",
                "start_time": 1700000000000000000,
                "end_time": 1700000001000000000,
                "attributes": {
                    "gen_ai.tool.name": "web_search",
                },
                "status": {"status_code": "OK"},
            },
        ]
        log = ExecutionLog.from_otel_export(spans)
        assert len(log) == 1
        assert log.entries[0].action == "tool_call"

    def test_from_otel_error_span(self) -> None:
        spans = [
            {
                "name": "llm.generate",
                "start_time": 0,
                "end_time": 0,
                "attributes": {},
                "status": {"status_code": "ERROR"},
            },
        ]
        log = ExecutionLog.from_otel_export(spans)
        assert len(log) == 1
        assert log.entries[0].success is False

    def test_from_otel_malformed_span_skipped(self) -> None:
        spans = [
            {"garbage": True},  # Missing required fields
            {
                "name": "valid",
                "start_time": 0,
                "end_time": 0,
                "attributes": {},
                "status": {},
            },
        ]
        log = ExecutionLog.from_otel_export(spans)
        # Both should parse (the first one has defaults for missing fields)
        assert len(log) >= 1

    def test_empty_log(self) -> None:
        log = ExecutionLog()
        assert len(log) == 0
        assert log.entries == []


# ---------------------------------------------------------------------------
# SimulationReport tests
# ---------------------------------------------------------------------------


class TestSimulationReport:
    def test_savings_percentage_zero_cost(self) -> None:
        report = SimulationReport(total_cost=0.0, cost_saved_estimate=0.0)
        assert report.savings_percentage == 0.0

    def test_savings_percentage_with_cost(self) -> None:
        report = SimulationReport(
            total_cost=100.0,
            cost_saved_estimate=25.0,
        )
        assert report.savings_percentage == 25.0

    def test_summary_format(self) -> None:
        report = SimulationReport(
            total_entries=10,
            allowed_count=7,
            halted_count=2,
            degraded_count=1,
            warned_count=0,
            total_cost=50.0,
            cost_saved_estimate=10.0,
        )
        s = report.summary()
        assert "10 entries" in s
        assert "$10.00" in s
        assert "20.0%" in s

    def test_to_dict_roundtrip(self) -> None:
        report = SimulationReport(
            total_entries=5,
            allowed_count=3,
            halted_count=1,
            degraded_count=1,
            total_cost=10.0,
            cost_saved_estimate=2.0,
            timeline=[
                SimulationEvent(
                    timestamp=1.0,
                    agent_id="bot",
                    action="llm_call",
                    decision=Decision.HALT,
                    cost_usd=2.0,
                    reason="test",
                    entry_index=0,
                ),
            ],
        )
        d = report.to_dict()
        assert d["total_entries"] == 5
        assert d["halted_count"] == 1
        assert len(d["timeline"]) == 1
        assert d["timeline"][0]["decision"] == "HALT"

        # JSON-serializable
        json.dumps(d)


# ---------------------------------------------------------------------------
# PolicySimulator tests
# ---------------------------------------------------------------------------


class _AlwaysAllowHook:
    """Stub hook that always allows."""

    def before_llm_call(self, ctx: ToolCallContext) -> Decision:
        return Decision.ALLOW


class _AlwaysHaltHook:
    """Stub hook that always halts."""

    def before_llm_call(self, ctx: ToolCallContext) -> Decision:
        return Decision.HALT


class _BudgetHaltHook:
    """Stub budget hook that halts when cumulative cost > threshold."""

    def __init__(self, threshold: float) -> None:
        self._threshold = threshold
        self._spent = 0.0

    def before_charge(self, ctx: ToolCallContext, cost_usd: float) -> Decision:
        self._spent += cost_usd
        if self._spent > self._threshold:
            return Decision.HALT
        return Decision.ALLOW


class TestPolicySimulator:
    def _make_entries(self, n: int, cost: float = 0.01) -> list[ExecutionLogEntry]:
        return [
            ExecutionLogEntry(
                timestamp=float(i),
                agent_id="bot",
                action="llm_call",
                cost_usd=cost,
                tokens=100,
            )
            for i in range(n)
        ]

    def test_all_allowed(self) -> None:
        pipeline = ShieldPipeline()  # No hooks = allow all
        sim = PolicySimulator(pipeline)
        report = sim.simulate(self._make_entries(5))
        assert report.total_entries == 5
        assert report.allowed_count == 5
        assert report.halted_count == 0
        assert report.cost_saved_estimate == 0.0

    def test_all_halted(self) -> None:
        pipeline = ShieldPipeline(pre_dispatch=_AlwaysHaltHook())
        sim = PolicySimulator(pipeline)
        report = sim.simulate(self._make_entries(5))
        assert report.halted_count == 5
        assert report.allowed_count == 0
        assert report.cost_saved_estimate == pytest.approx(0.05, abs=1e-9)

    def test_budget_halt_after_threshold(self) -> None:
        pipeline = ShieldPipeline(budget=_BudgetHaltHook(threshold=0.03))
        sim = PolicySimulator(pipeline)
        entries = self._make_entries(5, cost=0.01)
        report = sim.simulate(entries)
        # First 3 entries: spend accumulates to 0.01, 0.02, 0.03 (allowed)
        # 4th entry: 0.04 > 0.03 -> HALT
        # 5th entry: 0.05 > 0.03 -> HALT
        assert report.allowed_count == 3
        assert report.halted_count == 2
        assert report.cost_saved_estimate == pytest.approx(0.02, abs=1e-9)

    def test_tool_call_entries(self) -> None:
        pipeline = ShieldPipeline()
        sim = PolicySimulator(pipeline)
        entries = [
            ExecutionLogEntry(
                timestamp=1.0, agent_id="bot", action="tool_call", cost_usd=0.02
            ),
        ]
        report = sim.simulate(entries)
        assert report.allowed_count == 1

    def test_failed_entry_triggers_on_error(self) -> None:
        # Default pipeline with no retry hook -> on_error returns HALT (fail-closed)
        pipeline = ShieldPipeline()
        sim = PolicySimulator(pipeline)
        entries = [
            ExecutionLogEntry(
                timestamp=1.0,
                agent_id="bot",
                action="llm_call",
                success=False,
                cost_usd=0.05,
            ),
        ]
        report = sim.simulate(entries)
        assert report.halted_count == 1
        assert report.cost_saved_estimate == pytest.approx(0.05, abs=1e-9)

    def test_agent_breakdown(self) -> None:
        pipeline = ShieldPipeline()
        sim = PolicySimulator(pipeline)
        entries = [
            ExecutionLogEntry(timestamp=1.0, agent_id="agent-a", action="llm_call", cost_usd=0.1),
            ExecutionLogEntry(timestamp=2.0, agent_id="agent-b", action="llm_call", cost_usd=0.2),
            ExecutionLogEntry(timestamp=3.0, agent_id="agent-a", action="llm_call", cost_usd=0.3),
        ]
        report = sim.simulate(entries)
        assert "agent-a" in report.agent_breakdown
        assert "agent-b" in report.agent_breakdown
        assert report.agent_breakdown["agent-a"]["total"] == 2
        assert report.agent_breakdown["agent-b"]["total"] == 1
        assert report.agent_breakdown["agent-a"]["total_cost"] == pytest.approx(0.4)

    def test_empty_log(self) -> None:
        pipeline = ShieldPipeline()
        sim = PolicySimulator(pipeline)
        report = sim.simulate([])
        assert report.total_entries == 0
        assert report.allowed_count == 0
        assert report.total_cost == 0.0

    def test_timeline_ordering(self) -> None:
        pipeline = ShieldPipeline()
        sim = PolicySimulator(pipeline)
        entries = self._make_entries(3)
        report = sim.simulate(entries)
        assert len(report.timeline) == 3
        assert report.timeline[0].entry_index == 0
        assert report.timeline[2].entry_index == 2

    def test_mixed_actions(self) -> None:
        pipeline = ShieldPipeline()
        sim = PolicySimulator(pipeline)
        entries = [
            ExecutionLogEntry(timestamp=1.0, agent_id="a", action="llm_call", cost_usd=0.01),
            ExecutionLogEntry(timestamp=2.0, agent_id="a", action="tool_call", cost_usd=0.02),
            ExecutionLogEntry(timestamp=3.0, agent_id="a", action="reply", cost_usd=0.0),
        ]
        report = sim.simulate(entries)
        assert report.total_entries == 3
        assert report.allowed_count == 3


# ---------------------------------------------------------------------------
# Integration: PolicyLoader + Simulator
# ---------------------------------------------------------------------------


class TestSimulatorWithPolicyLoader:
    def test_yaml_policy_simulation(self, tmp_path: Path) -> None:
        """Full integration: YAML policy -> ShieldPipeline -> simulate log."""
        try:
            import yaml  # noqa: F401
        except ImportError:
            pytest.skip("pyyaml not installed")

        from veronica_core.policy.loader import PolicyLoader

        policy_yaml = """\
version: "1"
name: "test-sim-policy"
rules:
  - type: token_budget
    params:
      max_output_tokens: 1000
      max_total_tokens: 5000
"""
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(policy_yaml)

        loader = PolicyLoader()
        loaded = loader.load(policy_file)

        entries = [
            ExecutionLogEntry(
                timestamp=float(i),
                agent_id="bot",
                action="llm_call",
                cost_usd=0.01,
                tokens=100,
            )
            for i in range(10)
        ]

        sim = PolicySimulator(loaded.pipeline)
        report = sim.simulate(entries)
        # With a token_budget hook as pre_dispatch, all entries should be processed
        assert report.total_entries == 10
        assert report.total_cost == pytest.approx(0.10, abs=1e-9)
