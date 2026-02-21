"""VERONICA AIcontainer - Composite safety container for LLM agent calls.

Composes BudgetEnforcer, CircuitBreaker, RetryContainer, AgentStepGuard,
and PartialResultBuffer into a single check-and-reset boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from veronica_core.agent_guard import AgentStepGuard
from veronica_core.budget import BudgetEnforcer
from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.partial import PartialResultBuffer
from veronica_core.retry import RetryContainer
from veronica_core.runtime_policy import PolicyContext, PolicyDecision, PolicyPipeline
from veronica_core.semantic import SemanticLoopGuard


@dataclass
class AIcontainer:
    """Composite safety container for a single AI agent invocation boundary.

    Assembles independent safety primitives into one unified PolicyPipeline.
    All primitives are optional; omitted primitives are simply not evaluated.

    Example::

        from veronica_core import BudgetEnforcer, CircuitBreaker
        from veronica_core.container import AIcontainer

        container = AIcontainer(
            budget=BudgetEnforcer(limit_usd=5.0),
            circuit_breaker=CircuitBreaker(failure_threshold=3),
        )

        decision = container.check(cost_usd=0.50, step_count=1)
        if not decision.allowed:
            raise RuntimeError(decision.reason)

        # After successful call:
        container.circuit_breaker.record_success()

        # Reset all primitives between agent runs:
        container.reset()
    """

    budget: Optional[BudgetEnforcer] = None
    circuit_breaker: Optional[CircuitBreaker] = None
    retry: Optional[RetryContainer] = None
    step_guard: Optional[AgentStepGuard] = None
    partial_buffer: Optional[PartialResultBuffer] = None
    semantic_guard: Optional[SemanticLoopGuard] = None

    _pipeline: PolicyPipeline = field(init=False, repr=False)

    def __post_init__(self) -> None:
        primitives = [
            p
            for p in (
                self.budget,
                self.circuit_breaker,
                self.retry,
                self.step_guard,
                self.semantic_guard,
            )
            if p is not None
        ]
        self._pipeline = PolicyPipeline(primitives)  # type: ignore[arg-type]

    def check(
        self,
        cost_usd: float = 0.0,
        step_count: int = 0,
        entity_id: str = "",
        chain_id: str = "",
    ) -> PolicyDecision:
        """Evaluate all active policies against the given context.

        Args:
            cost_usd: Projected cost of the upcoming LLM call in USD.
            step_count: Current agent step number.
            entity_id: Identifier for the requesting entity (user, agent, etc.).
            chain_id: Identifier for the current execution chain.

        Returns:
            PolicyDecision -- denied by the first failing policy,
            or allowed if all active policies pass.
        """
        context = PolicyContext(
            cost_usd=cost_usd,
            step_count=step_count,
            entity_id=entity_id,
            chain_id=chain_id,
        )
        return self._pipeline.evaluate(context)

    def reset(self) -> None:
        """Reset all active primitives to their initial state.

        Calls reset() on each non-None primitive and rebuilds the pipeline.
        Use between agent runs to clear accumulated state.
        """
        for primitive in (
            self.budget,
            self.circuit_breaker,
            self.retry,
            self.step_guard,
            self.semantic_guard,
        ):
            if primitive is not None:
                primitive.reset()

        if self.partial_buffer is not None:
            self.partial_buffer.clear()

        # Rebuild pipeline (reset does not change membership)
        self.__post_init__()

    @property
    def active_policies(self) -> List[str]:
        """Policy type strings for all active (non-None) primitives.

        Returns:
            Ordered list of policy_type strings, e.g. ['budget', 'circuit_breaker'].
        """
        return [p.policy_type for p in self._pipeline.policies]
