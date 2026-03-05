"""PolicySimulator — replay execution logs against a ShieldPipeline.

Processes each log entry chronologically, evaluating the pipeline's
before_llm_call / before_tool_call / before_charge hooks as appropriate.
Produces a SimulationReport with aggregate statistics and a timeline.

Zero external dependencies.
"""

from __future__ import annotations

import logging
import math
from typing import Any
from uuid import uuid4

from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.types import Decision, ToolCallContext
from veronica_core.simulation.log import ExecutionLogEntry
from veronica_core.simulation.report import SimulationEvent, SimulationReport

logger = logging.getLogger(__name__)


class PolicySimulator:
    """Replays an execution log against a ShieldPipeline.

    For each log entry, constructs a ToolCallContext and evaluates the
    pipeline's hooks. The resulting Decision is recorded in the timeline.

    The pipeline is NOT mutated; internal state (step counters, budget
    trackers) does accumulate across entries just as they would in a live
    run.

    Args:
        pipeline: The ShieldPipeline to evaluate entries against.

    Example::

        from veronica_core.simulation import PolicySimulator, ExecutionLog
        from veronica_core.policy.loader import PolicyLoader

        loader = PolicyLoader()
        policy = loader.load("strict_policy.yaml")
        log = ExecutionLog.from_file("last_week.json")
        report = PolicySimulator(policy.pipeline).simulate(log.entries)
        print(report.summary())
    """

    def __init__(self, pipeline: ShieldPipeline) -> None:
        self._pipeline = pipeline

    def simulate(self, entries: list[ExecutionLogEntry]) -> SimulationReport:
        """Replay *entries* against the pipeline and return a report.

        Entries are processed in the order given (caller should sort by
        timestamp if needed).

        Args:
            entries: Ordered list of log entries to replay.

        Returns:
            SimulationReport with aggregate stats and per-entry timeline.
        """
        report = SimulationReport()
        report.total_entries = len(entries)
        agent_stats: dict[str, dict[str, Any]] = {}

        for idx, entry in enumerate(entries):
            ctx = _build_context(entry)
            decision = self._evaluate(entry, ctx, idx)
            reason = _decision_reason(decision, entry)

            event = SimulationEvent(
                timestamp=entry.timestamp,
                agent_id=entry.agent_id,
                action=entry.action,
                decision=decision,
                cost_usd=entry.cost_usd,
                reason=reason,
                entry_index=idx,
            )
            report.timeline.append(event)
            safe_cost = entry.cost_usd if math.isfinite(entry.cost_usd) else 0.0
            report.total_cost += safe_cost

            # Update counters
            if decision == Decision.ALLOW:
                report.allowed_count += 1
            elif decision == Decision.HALT:
                report.halted_count += 1
                report.cost_saved_estimate += safe_cost
            elif decision == Decision.DEGRADE:
                report.degraded_count += 1
            else:
                # WARN, QUEUE, RETRY, QUARANTINE all count as warned
                report.warned_count += 1

            # Per-agent breakdown
            _update_agent_stats(agent_stats, entry, decision)

        report.agent_breakdown = agent_stats
        return report

    def _evaluate(self, entry: ExecutionLogEntry, ctx: ToolCallContext, idx: int = 0) -> Decision:
        """Run the appropriate pipeline hook for this entry type."""
        try:
            if entry.action == "tool_call":
                result = self._pipeline.before_tool_call(ctx)
                if result != Decision.ALLOW:
                    return result

            # LLM calls and replies go through before_llm_call
            if entry.action in ("llm_call", "reply"):
                result = self._pipeline.before_llm_call(ctx)
                if result != Decision.ALLOW:
                    return result

            # Budget check for all entries with positive cost.
            # Intentional: zero-cost entries (cached responses, no-cost tools)
            # skip before_charge.  Pipeline hooks that track call counts rather
            # than cost will not be invoked for these entries during simulation.
            # NaN entries are safe: NaN > 0 evaluates to False, so they are skipped.
            if entry.cost_usd > 0:
                result = self._pipeline.before_charge(ctx, entry.cost_usd)
                if result != Decision.ALLOW:
                    return result

            # Simulate error path for failed entries
            if not entry.success:
                result = self._pipeline.on_error(ctx, RuntimeError("simulated failure"))
                if result != Decision.ALLOW:
                    return result

            return Decision.ALLOW
        except Exception:
            logger.debug(
                "Simulator: pipeline evaluation failed for entry %s", idx,
                exc_info=True,
            )
            return Decision.HALT


def _build_context(entry: ExecutionLogEntry) -> ToolCallContext:
    """Build a ToolCallContext from a log entry."""
    return ToolCallContext(
        request_id=str(uuid4()),
        tool_name=entry.metadata.get("tool_name") if entry.action == "tool_call" else None,
        model=entry.model or None,
        tokens_in=entry.metadata.get("prompt_tokens"),
        tokens_out=entry.metadata.get("completion_tokens"),
        cost_usd=entry.cost_usd if entry.cost_usd > 0 else None,
        metadata={"agent_id": entry.agent_id, "simulated": True},
    )


def _decision_reason(decision: Decision, entry: ExecutionLogEntry) -> str:
    """Generate a human-readable reason for the decision."""
    if decision == Decision.ALLOW:
        return "Policy allowed"
    return (
        f"{decision.value}: {entry.action} by {entry.agent_id} "
        f"(cost=${entry.cost_usd:.4f}, tokens={entry.tokens})"
    )


def _update_agent_stats(
    stats: dict[str, dict[str, Any]],
    entry: ExecutionLogEntry,
    decision: Decision,
) -> None:
    """Update per-agent statistics."""
    agent = entry.agent_id
    if agent not in stats:
        stats[agent] = {
            "total": 0,
            "allowed": 0,
            "halted": 0,
            "degraded": 0,
            "warned": 0,
            "total_cost": 0.0,
            "cost_saved": 0.0,
        }
    safe_cost = entry.cost_usd if math.isfinite(entry.cost_usd) else 0.0
    s = stats[agent]
    s["total"] += 1
    s["total_cost"] += safe_cost
    if decision == Decision.ALLOW:
        s["allowed"] += 1
    elif decision == Decision.HALT:
        s["halted"] += 1
        s["cost_saved"] += safe_cost
    elif decision == Decision.DEGRADE:
        s["degraded"] += 1
    else:
        s["warned"] += 1
