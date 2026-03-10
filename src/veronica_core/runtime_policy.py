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

__all__ = [
    "PolicyContext",
    "PolicyDecision",
    "RuntimePolicy",
    "PolicyPipeline",
    "allow",
    "deny",
    "model_downgrade",
    "rate_limit_decision",
]

from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol, runtime_checkable
import logging
import threading
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
    # Degradation extensions (v0.10.0) -- all optional, default None/0
    degradation_action: str | None = (
        None  # "MODEL_DOWNGRADE" | "CONTEXT_TRIM" | "RATE_LIMIT"
    )
    fallback_model: str | None = None
    rate_limit_ms: int = 0


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

    Thread-safe: add() and evaluate() are protected by an internal lock.
    evaluate() copies the policy list before iteration to avoid holding the
    lock during policy execution (prevents deadlock if a policy calls add()).

    Example:
        pipeline = PolicyPipeline([
            BudgetEnforcer(limit_usd=10.0),
            AgentStepGuard(max_steps=25),
        ])
        decision = pipeline.evaluate(PolicyContext(cost_usd=1.50))
        if not decision.allowed:
            logger.info("Denied by %s: %s", decision.policy_type, decision.reason)
    """

    def __init__(self, policies: Optional[List[RuntimePolicy]] = None) -> None:
        self._policies: List[RuntimePolicy] = list(policies or [])
        # H6: Lock protecting _policies list for thread-safe add() and evaluate().
        self._lock = threading.Lock()

    def add(self, policy: RuntimePolicy) -> None:
        """Add a policy to the pipeline.

        Args:
            policy: RuntimePolicy instance to append
        """
        with self._lock:
            self._policies.append(policy)

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        """Evaluate all policies. First denial wins.

        Args:
            context: Current execution context

        Returns:
            PolicyDecision -- denied by first failing policy, or allowed if all pass
        """
        # H6: Copy the list under the lock to avoid RuntimeError if add() is called
        # concurrently. Policy execution happens outside the lock to prevent deadlock
        # in case a policy's check() method calls pipeline.add().
        with self._lock:
            policies = list(self._policies)
        for policy in policies:
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
        with self._lock:
            return list(self._policies)

    def __len__(self) -> int:
        with self._lock:
            return len(self._policies)


# ---------------------------------------------------------------------------
# PolicyDecision factory helpers (v0.10.0)
# ---------------------------------------------------------------------------


def allow(policy_type: str = "allow") -> PolicyDecision:
    """Return an allowed PolicyDecision."""
    return PolicyDecision(allowed=True, policy_type=policy_type)


def deny(policy_type: str, reason: str = "") -> PolicyDecision:
    """Return a denied PolicyDecision."""
    return PolicyDecision(allowed=False, policy_type=policy_type, reason=reason)


def model_downgrade(
    current_model: str,
    fallback_model: str,
    reason: str = "",
) -> PolicyDecision:
    """Return a MODEL_DOWNGRADE degradation decision."""
    return PolicyDecision(
        allowed=True,
        policy_type="model_downgrade",
        reason=reason,
        degradation_action="MODEL_DOWNGRADE",
        fallback_model=fallback_model,
    )


def rate_limit_decision(delay_ms: int, reason: str = "") -> PolicyDecision:
    """Return a RATE_LIMIT degradation decision."""
    return PolicyDecision(
        allowed=True,
        policy_type="rate_limit",
        reason=reason,
        degradation_action="RATE_LIMIT",
        rate_limit_ms=delay_ms,
    )
