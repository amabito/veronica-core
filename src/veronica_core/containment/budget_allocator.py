"""BudgetAllocator — multi-agent budget distribution for ExecutionContext.

Provides a Protocol and concrete implementations for distributing a parent
budget across named child agents. Integrates with ExecutionContext.create_child()
to allocate per-agent spending ceilings at spawn time.

Usage::

    from veronica_core.containment.budget_allocator import FairShareAllocator

    allocator = FairShareAllocator()
    result = allocator.allocate(
        total_budget=1.0,
        agent_names=["planner", "executor", "validator"],
        current_usage={"planner": 0.1, "executor": 0.0, "validator": 0.0},
    )
    # result.allocations == {"planner": 0.3, "executor": 0.3, "validator": 0.3}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


__all__ = [
    "AllocationResult",
    "BudgetAllocator",
    "FairShareAllocator",
    "WeightedAllocator",
    "DynamicAllocator",
]


# ---------------------------------------------------------------------------
# AllocationResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllocationResult:
    """Immutable result of a budget allocation pass.

    Attributes:
        allocations: Mapping of agent_name -> allocated budget in USD.
        total_allocated: Sum of all allocations. May be less than total_budget
            when budget is exhausted or no agents are given.
        total_remaining: total_budget minus total_allocated.
    """

    allocations: dict[str, float]
    total_allocated: float
    total_remaining: float


# ---------------------------------------------------------------------------
# BudgetAllocator Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BudgetAllocator(Protocol):
    """Protocol for distributing a parent budget across child agents.

    Implementations must be deterministic and thread-safe. They must not
    mutate any arguments. The returned AllocationResult must satisfy:

        sum(allocations.values()) == total_allocated
        total_allocated + total_remaining == total_budget
    """

    def allocate(
        self,
        total_budget: float,
        agent_names: list[str],
        current_usage: dict[str, float],
    ) -> AllocationResult:
        """Allocate *total_budget* across *agent_names*.

        Args:
            total_budget: Total USD budget available (parent ceiling).
            agent_names: Ordered list of agent names to allocate to.
            current_usage: Map of agent_name -> USD already spent. Agents not
                present in the map are treated as having spent 0.

        Returns:
            AllocationResult with per-agent allocations.
        """
        ...


# ---------------------------------------------------------------------------
# FairShareAllocator
# ---------------------------------------------------------------------------


class FairShareAllocator:
    """Equal split of remaining budget across all agents.

    Each agent receives an identical share of the total budget regardless of
    current usage. Useful when agents have similar cost profiles and you want
    to prevent any single agent from consuming the entire budget.

    If budget is zero or negative, all agents receive 0.
    If no agents are given, returns an empty allocation.
    """

    def allocate(
        self,
        total_budget: float,
        agent_names: list[str],
        current_usage: dict[str, float],
    ) -> AllocationResult:
        if not agent_names or total_budget <= 0.0:
            return AllocationResult(
                allocations={name: 0.0 for name in agent_names},
                total_allocated=0.0,
                total_remaining=max(0.0, total_budget),
            )

        share = total_budget / len(agent_names)
        allocations = {name: share for name in agent_names}
        total_allocated = share * len(agent_names)
        return AllocationResult(
            allocations=allocations,
            total_allocated=total_allocated,
            total_remaining=total_budget - total_allocated,
        )


# ---------------------------------------------------------------------------
# WeightedAllocator
# ---------------------------------------------------------------------------


class WeightedAllocator:
    """Weighted allocation based on declared priority weights.

    Agents with higher weights receive proportionally larger shares.
    Weights are automatically normalized so they sum to 1.0.
    Agents not in the weights dict receive 0 allocation.

    Args:
        weights: Mapping of agent_name -> relative priority weight. All
            values must be non-negative. At least one must be positive.
    """

    def __init__(self, weights: dict[str, float]) -> None:
        if any(w < 0.0 for w in weights.values()):
            raise ValueError("All weights must be non-negative.")
        self._weights = dict(weights)

    def allocate(
        self,
        total_budget: float,
        agent_names: list[str],
        current_usage: dict[str, float],
    ) -> AllocationResult:
        if not agent_names or total_budget <= 0.0:
            return AllocationResult(
                allocations={name: 0.0 for name in agent_names},
                total_allocated=0.0,
                total_remaining=max(0.0, total_budget),
            )

        # Only count weights for agents in agent_names.
        relevant = {name: self._weights.get(name, 0.0) for name in agent_names}
        total_weight = sum(relevant.values())

        if total_weight == 0.0:
            # All weights zero — fall back to equal split.
            share = total_budget / len(agent_names)
            allocations = {name: share for name in agent_names}
            total_allocated = share * len(agent_names)
        else:
            allocations = {
                name: (w / total_weight) * total_budget for name, w in relevant.items()
            }
            total_allocated = sum(allocations.values())

        return AllocationResult(
            allocations=allocations,
            total_allocated=total_allocated,
            total_remaining=total_budget - total_allocated,
        )


# ---------------------------------------------------------------------------
# DynamicAllocator
# ---------------------------------------------------------------------------


class DynamicAllocator:
    """Reallocates budget based on actual usage patterns.

    Agents that consume less of their previous allocation receive less next
    round; agents that use more receive more. This prevents over-provisioning
    idle agents and lets high-consumption agents scale up.

    With no prior usage data (all zeros), falls back to equal split.

    A *min_share* floor ensures every agent receives at least that fraction of
    the total budget, preventing starvation of infrequently-used agents.

    Args:
        min_share: Minimum fraction of total_budget guaranteed to each agent.
            Must be in [0.0, 1.0). Defaults to 0.05 (5%).
    """

    def __init__(self, min_share: float = 0.05) -> None:
        if not (0.0 <= min_share < 1.0):
            raise ValueError("min_share must be in [0.0, 1.0).")
        self._min_share = min_share

    def allocate(
        self,
        total_budget: float,
        agent_names: list[str],
        current_usage: dict[str, float],
    ) -> AllocationResult:
        if not agent_names or total_budget <= 0.0:
            return AllocationResult(
                allocations={name: 0.0 for name in agent_names},
                total_allocated=0.0,
                total_remaining=max(0.0, total_budget),
            )

        n = len(agent_names)
        min_floor = self._min_share * total_budget

        # Clamp negative usage to zero (defensive against corrupted state).
        usage = {name: max(0.0, current_usage.get(name, 0.0)) for name in agent_names}
        total_usage = sum(usage.values())

        if total_usage < 1e-12:
            # No meaningful usage signal (zero or near-zero float noise) — equal split.
            share = total_budget / n
            allocations = {name: share for name in agent_names}
        else:
            # Proportional to usage, then apply min_share floor.
            # First reserve the floor for each agent.
            reserved = min_floor * n
            if reserved > total_budget:
                # Floor exceeds budget; scale down to fit.
                min_floor = total_budget / n
                reserved = total_budget
            remaining_for_proportional = max(0.0, total_budget - reserved)

            # Proportional allocation of the non-floor portion.
            proportional: dict[str, float] = {
                name: (usage[name] / total_usage) * remaining_for_proportional
                for name in agent_names
            }

            # Add floor back.
            allocations = {name: min_floor + proportional[name] for name in agent_names}

        total_allocated = sum(allocations.values())
        return AllocationResult(
            allocations=allocations,
            total_allocated=total_allocated,
            total_remaining=total_budget - total_allocated,
        )
