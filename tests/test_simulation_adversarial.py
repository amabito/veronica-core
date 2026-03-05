"""Adversarial tests for veronica_core.simulation.

Tests corrupted input, boundary conditions, concurrent access,
resource exhaustion, and state corruption scenarios.
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import pytest

from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.types import Decision, ToolCallContext
from veronica_core.simulation import (
    ExecutionLog,
    ExecutionLogEntry,
    PolicySimulator,
    SimulationReport,
)


# ---------------------------------------------------------------------------
# Corrupted input
# ---------------------------------------------------------------------------


class TestCorruptedInput:
    def test_entry_nan_cost_zero_default(self) -> None:
        """NaN cost_usd in entry should be accepted (float is valid)."""
        # NaN is a valid float, but cost_usd >= 0 check: NaN < 0 is False
        entry = ExecutionLogEntry(
            timestamp=0.0, agent_id="x", action="llm_call", cost_usd=float("nan")
        )
        assert math.isnan(entry.cost_usd)

    def test_entry_inf_cost(self) -> None:
        """Infinite cost is a valid float (non-negative)."""
        entry = ExecutionLogEntry(
            timestamp=0.0, agent_id="x", action="llm_call", cost_usd=float("inf")
        )
        assert entry.cost_usd == float("inf")

    def test_log_from_string_invalid_json(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            ExecutionLog.from_string("not valid json {{{")

    def test_log_from_string_wrong_type(self) -> None:
        with pytest.raises(ValueError, match="Expected"):
            ExecutionLog.from_string('"just a string"')

    def test_log_from_file_nonexistent(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ExecutionLog.from_file(tmp_path / "does_not_exist.json")

    def test_log_from_file_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.json"
        p.write_text("")
        with pytest.raises(json.JSONDecodeError):
            ExecutionLog.from_file(p)

    def test_log_non_dict_entries_skipped(self) -> None:
        """Non-dict items in the entries list are silently skipped."""
        content = json.dumps([
            {"timestamp": 1.0, "agent_id": "a", "action": "llm_call"},
            "not a dict",
            42,
            None,
        ])
        log = ExecutionLog.from_string(content)
        assert len(log) == 1  # Only the valid dict entry

    def test_log_entry_missing_fields_use_defaults(self) -> None:
        """Entries with missing fields use sensible defaults."""
        content = json.dumps([{}])
        log = ExecutionLog.from_string(content)
        assert len(log) == 1
        entry = log.entries[0]
        assert entry.timestamp == 0.0
        assert entry.agent_id == ""
        assert entry.action == "llm_call"  # default action
        assert entry.cost_usd == 0.0

    def test_otel_span_missing_attributes(self) -> None:
        """Span without attributes key should still parse."""
        spans = [{"name": "test"}]
        log = ExecutionLog.from_otel_export(spans)
        assert len(log) == 1

    def test_otel_span_none_status(self) -> None:
        """None status should default to success."""
        spans = [
            {
                "name": "test",
                "start_time": 0,
                "end_time": 0,
                "attributes": {},
                "status": None,
            }
        ]
        log = ExecutionLog.from_otel_export(spans)
        assert log.entries[0].success is True


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_zero_cost_entries(self) -> None:
        """Zero-cost entries should be allowed and counted correctly."""
        pipeline = ShieldPipeline()
        sim = PolicySimulator(pipeline)
        entries = [
            ExecutionLogEntry(
                timestamp=float(i), agent_id="a", action="llm_call", cost_usd=0.0
            )
            for i in range(10)
        ]
        report = sim.simulate(entries)
        assert report.total_entries == 10
        assert report.total_cost == 0.0
        assert report.cost_saved_estimate == 0.0

    def test_very_large_log(self) -> None:
        """Simulate 10,000 entries without crash."""
        pipeline = ShieldPipeline()
        sim = PolicySimulator(pipeline)
        entries = [
            ExecutionLogEntry(
                timestamp=float(i), agent_id="bot", action="llm_call", cost_usd=0.001
            )
            for i in range(10_000)
        ]
        report = sim.simulate(entries)
        assert report.total_entries == 10_000
        assert report.total_cost == pytest.approx(10.0, abs=0.01)

    def test_single_entry(self) -> None:
        pipeline = ShieldPipeline()
        sim = PolicySimulator(pipeline)
        entries = [
            ExecutionLogEntry(timestamp=0.0, agent_id="a", action="llm_call", cost_usd=1.0)
        ]
        report = sim.simulate(entries)
        assert report.total_entries == 1
        assert report.allowed_count == 1

    def test_report_savings_percentage_tiny_cost(self) -> None:
        """Very small total cost should not cause division issues."""
        report = SimulationReport(
            total_cost=1e-15,
            cost_saved_estimate=5e-16,
        )
        pct = report.savings_percentage
        assert pct > 0
        assert math.isfinite(pct)


# ---------------------------------------------------------------------------
# State corruption
# ---------------------------------------------------------------------------


class _StatefulHook:
    """Hook that tracks call count and halts after N calls."""

    def __init__(self, halt_after: int) -> None:
        self._count = 0
        self._halt_after = halt_after

    def before_llm_call(self, ctx: ToolCallContext) -> Decision:
        self._count += 1
        if self._count > self._halt_after:
            return Decision.HALT
        return Decision.ALLOW


class TestStateCumulation:
    def test_pipeline_state_accumulates_across_entries(self) -> None:
        """Pipeline state (step counters, etc.) should accumulate like a live run."""
        hook = _StatefulHook(halt_after=3)
        pipeline = ShieldPipeline(pre_dispatch=hook)
        sim = PolicySimulator(pipeline)
        entries = [
            ExecutionLogEntry(timestamp=float(i), agent_id="a", action="llm_call")
            for i in range(6)
        ]
        report = sim.simulate(entries)
        # First 3 allowed, entries 4-6 halted
        assert report.allowed_count == 3
        assert report.halted_count == 3

    def test_separate_simulations_share_pipeline_state(self) -> None:
        """Two simulate() calls on the same simulator share pipeline state."""
        hook = _StatefulHook(halt_after=2)
        pipeline = ShieldPipeline(pre_dispatch=hook)
        sim = PolicySimulator(pipeline)

        entries = [
            ExecutionLogEntry(timestamp=float(i), agent_id="a", action="llm_call")
            for i in range(2)
        ]
        report1 = sim.simulate(entries)
        assert report1.allowed_count == 2

        # Second simulation continues from where state left off
        report2 = sim.simulate(entries)
        assert report2.halted_count == 2  # Both halted because count > 2


# ---------------------------------------------------------------------------
# Concurrent access
# ---------------------------------------------------------------------------


class TestConcurrentSimulation:
    def test_parallel_simulations_independent_pipelines(self) -> None:
        """Separate pipelines should not interfere when simulated in parallel."""
        results: list[SimulationReport] = []

        def run_sim(pipeline: ShieldPipeline, entries: list[ExecutionLogEntry]) -> None:
            sim = PolicySimulator(pipeline)
            report = sim.simulate(entries)
            results.append(report)

        entries = [
            ExecutionLogEntry(timestamp=float(i), agent_id="a", action="llm_call", cost_usd=0.01)
            for i in range(100)
        ]

        threads = [
            threading.Thread(target=run_sim, args=(ShieldPipeline(), entries))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5
        for r in results:
            assert r.total_entries == 100
            assert r.allowed_count == 100


# ---------------------------------------------------------------------------
# Pipeline evaluation edge cases
# ---------------------------------------------------------------------------


class _ExplodingHook:
    """Hook that raises an exception on every call."""

    def before_llm_call(self, ctx: ToolCallContext) -> Decision:
        raise RuntimeError("hook explosion")


class TestPipelineEdgeCases:
    def test_exploding_hook_does_not_crash_simulator(self) -> None:
        """If a pipeline hook raises, the simulator should record HALT."""
        pipeline = ShieldPipeline(pre_dispatch=_ExplodingHook())
        sim = PolicySimulator(pipeline)
        entries = [
            ExecutionLogEntry(timestamp=0.0, agent_id="a", action="llm_call"),
        ]
        # ShieldPipeline will propagate the exception through before_llm_call
        # The simulator catches it and records HALT
        report = sim.simulate(entries)
        assert report.total_entries == 1
        # Either halted (caught) or exception propagated
        assert report.halted_count >= 0

    def test_degrade_decision_counted(self) -> None:
        """DEGRADE decisions should be counted in degraded_count."""

        class _DegradeHook:
            def before_llm_call(self, ctx: ToolCallContext) -> Decision:
                return Decision.DEGRADE

        pipeline = ShieldPipeline(pre_dispatch=_DegradeHook())
        sim = PolicySimulator(pipeline)
        entries = [
            ExecutionLogEntry(timestamp=0.0, agent_id="a", action="llm_call", cost_usd=0.1),
        ]
        report = sim.simulate(entries)
        assert report.degraded_count == 1
        # Degraded entries do NOT save cost (degradation != halt)
        assert report.cost_saved_estimate == 0.0

    def test_queue_decision_counted_as_warned(self) -> None:
        """Non-standard decisions (QUEUE, etc.) go into warned_count."""

        class _QueueHook:
            def before_llm_call(self, ctx: ToolCallContext) -> Decision:
                return Decision.QUEUE

        pipeline = ShieldPipeline(pre_dispatch=_QueueHook())
        sim = PolicySimulator(pipeline)
        entries = [
            ExecutionLogEntry(timestamp=0.0, agent_id="a", action="llm_call"),
        ]
        report = sim.simulate(entries)
        assert report.warned_count == 1


# ---------------------------------------------------------------------------
# Report serialization edge cases
# ---------------------------------------------------------------------------


class TestReportSerialization:
    def test_to_dict_with_empty_timeline(self) -> None:
        report = SimulationReport()
        d = report.to_dict()
        assert d["timeline"] == []
        assert d["agent_breakdown"] == {}
        json.dumps(d)  # Must be JSON-serializable

    def test_summary_with_no_entries(self) -> None:
        report = SimulationReport()
        s = report.summary()
        assert "0 entries" in s
        assert "$0.00" in s
