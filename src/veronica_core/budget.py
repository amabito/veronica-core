"""VERONICA Budget Enforcement - Chain-level cost ceiling for LLM calls."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict
import logging
import threading

from veronica_core.runtime_policy import PolicyContext, PolicyDecision

logger = logging.getLogger(__name__)


@dataclass
class BudgetEnforcer:
    """Chain-level budget enforcement for LLM API calls.

    Tracks cumulative cost across a chain of calls and stops
    when the budget ceiling is reached. Thread-safe.

    Example:
        budget = BudgetEnforcer(limit_usd=100.0)
        for call in llm_calls:
            if not budget.spend(call.cost):
                break  # Budget exceeded
    """

    limit_usd: float = 100.0
    _spent_usd: float = field(default=0.0, init=False)
    _call_count: int = field(default=0, init=False)
    _exceeded: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def spend(self, amount_usd: float) -> bool:
        """Record spending. Returns True if within budget, False if exceeded.

        Args:
            amount_usd: Cost of this call in USD

        Returns:
            True if still within budget after this spend

        Raises:
            ValueError: If amount_usd is negative.
        """
        with self._lock:
            if amount_usd < 0:
                raise ValueError(
                    f"amount must be non-negative, got {amount_usd}"
                )
            projected = self._spent_usd + amount_usd
            if projected > self.limit_usd:
                logger.warning(
                    f"[VERONICA_BUDGET] Budget exceeded: "
                    f"${projected:.2f} / ${self.limit_usd:.2f} "
                    f"({self._call_count} calls)"
                )
                self._exceeded = True
                return False
            self._spent_usd = projected
            self._call_count += 1
            return True

    @property
    def spent_usd(self) -> float:
        """Total amount spent so far."""
        with self._lock:
            return self._spent_usd

    @property
    def remaining_usd(self) -> float:
        """Remaining budget in USD. Returns 0.0 if budget has been exceeded."""
        with self._lock:
            if self._exceeded:
                return 0.0
            return max(0.0, self.limit_usd - self._spent_usd)

    @property
    def is_exceeded(self) -> bool:
        """True if a spend attempt has been rejected due to the budget limit."""
        with self._lock:
            return self._exceeded

    @property
    def call_count(self) -> int:
        """Total number of calls tracked."""
        with self._lock:
            return self._call_count

    @property
    def utilization(self) -> float:
        """Budget utilization as a fraction (0.0 to 1.0+)."""
        with self._lock:
            if self.limit_usd <= 0:
                return float("inf")
            return self._spent_usd / self.limit_usd

    def reset(self) -> None:
        """Reset budget tracking."""
        with self._lock:
            self._spent_usd = 0.0
            self._call_count = 0
            self._exceeded = False
            logger.info("[VERONICA_BUDGET] Budget reset")

    @property
    def policy_type(self) -> str:
        """RuntimePolicy protocol: policy type identifier."""
        return "budget"

    def check(self, context: PolicyContext) -> PolicyDecision:
        """RuntimePolicy protocol: check if operation is within budget.

        Evaluates whether the projected cost (spent + context.cost_usd)
        would exceed the budget limit. Does NOT record spending --
        use spend() to record actual costs after the operation.

        Args:
            context: PolicyContext with cost_usd set to projected cost

        Returns:
            PolicyDecision allowing or denying the operation
        """
        with self._lock:
            if self._exceeded:
                return PolicyDecision(
                    allowed=False,
                    policy_type=self.policy_type,
                    reason=(
                        f"Budget exceeded: "
                        f"${self._spent_usd:.2f} / ${self.limit_usd:.2f}"
                    ),
                )
            projected = self._spent_usd + context.cost_usd
            if projected > self.limit_usd:
                return PolicyDecision(
                    allowed=False,
                    policy_type=self.policy_type,
                    reason=(
                        f"Budget would exceed: "
                        f"${projected:.2f} > ${self.limit_usd:.2f}"
                    ),
                )
            return PolicyDecision(allowed=True, policy_type=self.policy_type)

    def to_dict(self) -> Dict:
        """Serialize budget state."""
        with self._lock:
            return {
                "limit_usd": self.limit_usd,
                "spent_usd": self._spent_usd,
                "call_count": self._call_count,
            }
