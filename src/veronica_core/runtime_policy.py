"""VERONICA Runtime Policy Control - Protocol definitions for LLM runtime policies.

This module defines the core abstractions for Runtime Policy Control:
- RuntimePolicy: Protocol that all policy primitives implement
- PolicyContext: Context passed to policy checks
- PolicyDecision: Result of a policy evaluation
- PolicyPipeline: AND-composition of multiple policies (first denial wins)

These abstractions allow composing arbitrary runtime constraints
(budget, step limits, circuit breakers, rate limiters, etc.)
into a unified evaluation pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol, runtime_checkable
import logging
import time

logger = logging.getLogger(__name__)


@dataclass
class PolicyContext:
    """Context passed to RuntimePolicy.check() for evaluation.

    Carries ambient information about the current LLM call or agent step.
    Policies inspect relevant fields and ignore the rest.

    Example:
        ctx = PolicyContext(cost_usd=0.03, step_count=5, entity_id="user-123")
    """

    cost_usd: float = 0.0
    step_count: int = 0
    entity_id: str = ""
    chain_id: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


@dataclass
class PolicyDecision:
    """Result of a RuntimePolicy.check() evaluation.

    allowed=True means this policy permits the operation.
    allowed=False means this policy denies it, with reason and optional partial result.

    Example:
        PolicyDecision(allowed=False, policy_type="budget", reason="Over $10 limit")
    """

    allowed: bool
    policy_type: str
    reason: str = ""
    partial_result: Any = None


@runtime_checkable
class RuntimePolicy(Protocol):
    """Protocol for runtime policy primitives.

    Any class implementing check(), reset(), and policy_type
    is a valid RuntimePolicy (structural subtyping via Protocol).

    Existing classes (BudgetEnforcer, AgentStepGuard, etc.) implement
    this protocol without changing their existing APIs.
    """

    def check(self, context: PolicyContext) -> PolicyDecision:
        """Evaluate whether the operation should be allowed.

        Args:
            context: Current execution context

        Returns:
            PolicyDecision with allowed=True/False
        """
        ...

    def reset(self) -> None:
        """Reset policy state to initial conditions."""
        ...

    @property
    def policy_type(self) -> str:
        """Unique identifier for this policy type (e.g., 'budget', 'step_limit')."""
        ...


class PolicyPipeline:
    """AND-composition of RuntimePolicy instances.

    Evaluates policies in order. First denial stops evaluation
    and returns the denying decision. If all pass, returns allowed.

    No override mechanism -- if any policy denies, the operation is denied.

    Example:
        pipeline = PolicyPipeline([
            BudgetEnforcer(limit_usd=10.0),
            AgentStepGuard(max_steps=25),
        ])
        decision = pipeline.evaluate(PolicyContext(cost_usd=1.50))
        if not decision.allowed:
            print(f"Denied by {decision.policy_type}: {decision.reason}")
    """

    def __init__(self, policies: Optional[List[RuntimePolicy]] = None) -> None:
        self._policies: List[RuntimePolicy] = list(policies or [])

    def add(self, policy: RuntimePolicy) -> None:
        """Add a policy to the pipeline.

        Args:
            policy: RuntimePolicy instance to append
        """
        self._policies.append(policy)

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        """Evaluate all policies. First denial wins.

        Args:
            context: Current execution context

        Returns:
            PolicyDecision -- denied by first failing policy, or allowed if all pass
        """
        for policy in self._policies:
            decision = policy.check(context)
            if not decision.allowed:
                logger.info(
                    f"[VERONICA_POLICY] Denied by {decision.policy_type}: "
                    f"{decision.reason}"
                )
                return decision

        return PolicyDecision(
            allowed=True,
            policy_type="pipeline",
            reason="All policies passed",
        )

    @property
    def policies(self) -> List[RuntimePolicy]:
        """List of policies in evaluation order (copy)."""
        return list(self._policies)

    def __len__(self) -> int:
        return len(self._policies)
