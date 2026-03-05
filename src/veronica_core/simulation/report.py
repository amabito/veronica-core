"""Simulation report models for policy what-if analysis.

SimulationEvent records a single policy decision made during log replay.
SimulationReport aggregates all events into summary statistics.

Zero external dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from veronica_core.shield.types import Decision


@dataclass(frozen=True)
class SimulationEvent:
    """Single policy decision during a simulation run.

    Attributes:
        timestamp:  Timestamp of the original log entry.
        agent_id:   Agent that would have been affected.
        action:     Original action type ("llm_call", "tool_call", "reply").
        decision:   Policy decision that would have been made.
        cost_usd:   Cost of the original action (would be saved if HALT).
        reason:     Human-readable explanation of the decision.
        entry_index: Index of the entry in the original log.
    """

    timestamp: float
    agent_id: str
    action: str
    decision: Decision
    cost_usd: float
    reason: str
    entry_index: int = 0


@dataclass
class SimulationReport:
    """Summary of a policy simulation run.

    Attributes:
        total_entries:       Total log entries processed.
        allowed_count:       Entries that would have been allowed.
        halted_count:        Entries that would have been halted.
        degraded_count:      Entries that would have resulted in degradation.
        warned_count:        Entries that triggered warnings.
        cost_saved_estimate: Estimated USD savings from halted entries.
        total_cost:          Total USD cost in the original log.
        timeline:            Chronological list of all simulation events.
        agent_breakdown:     Per-agent summary statistics.
    """

    total_entries: int = 0
    allowed_count: int = 0
    halted_count: int = 0
    degraded_count: int = 0
    warned_count: int = 0
    cost_saved_estimate: float = 0.0
    total_cost: float = 0.0
    timeline: list[SimulationEvent] = field(default_factory=list)
    agent_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def savings_percentage(self) -> float:
        """Percentage of cost that would have been saved."""
        if self.total_cost <= 0:
            return 0.0
        return (self.cost_saved_estimate / self.total_cost) * 100.0

    def summary(self) -> str:
        """Human-readable one-paragraph summary."""
        return (
            f"Simulated {self.total_entries} entries: "
            f"{self.allowed_count} allowed, "
            f"{self.halted_count} halted, "
            f"{self.degraded_count} degraded, "
            f"{self.warned_count} warned. "
            f"Estimated savings: ${self.cost_saved_estimate:.2f} "
            f"of ${self.total_cost:.2f} ({self.savings_percentage:.1f}%)."
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON export."""
        return {
            "total_entries": self.total_entries,
            "allowed_count": self.allowed_count,
            "halted_count": self.halted_count,
            "degraded_count": self.degraded_count,
            "warned_count": self.warned_count,
            "cost_saved_estimate": self.cost_saved_estimate,
            "total_cost": self.total_cost,
            "savings_percentage": self.savings_percentage,
            "agent_breakdown": self.agent_breakdown,
            "timeline": [
                {
                    "timestamp": e.timestamp,
                    "agent_id": e.agent_id,
                    "action": e.action,
                    "decision": e.decision.value,
                    "cost_usd": e.cost_usd,
                    "reason": e.reason,
                    "entry_index": e.entry_index,
                }
                for e in self.timeline
            ],
        }
