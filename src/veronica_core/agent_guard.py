"""VERONICA Agent Step Guard - Limits autonomous agent iterations."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Any
import logging

from veronica_core.runtime_policy import PolicyContext, PolicyDecision

logger = logging.getLogger(__name__)


@dataclass
class AgentStepGuard:
    """Limits the number of steps an autonomous agent can take.

    Prevents runaway agent loops by enforcing a hard step limit.
    When exceeded, the last partial result is preserved.

    Example:
        guard = AgentStepGuard(max_steps=25)
        while guard.step(result=partial_output):
            partial_output = agent.next_action()
        final = guard.last_result  # Partial result preserved
    """

    max_steps: int = 25
    _current_step: int = field(default=0, init=False)
    _last_result: Optional[Any] = field(default=None, init=False, repr=False)

    def step(self, result: Any = None) -> bool:
        """Record one agent step. Returns True if more steps allowed.

        Args:
            result: Partial result from this step (preserved on limit)

        Returns:
            True if more steps allowed, False if limit reached
        """
        self._current_step += 1
        if result is not None:
            self._last_result = result

        if self._current_step >= self.max_steps:
            logger.warning(
                f"[VERONICA_AGENT] Step limit reached: "
                f"{self._current_step}/{self.max_steps}"
            )
            return False

        return True

    @property
    def current_step(self) -> int:
        """Current step count."""
        return self._current_step

    @property
    def remaining_steps(self) -> int:
        """Remaining steps before limit."""
        return max(0, self.max_steps - self._current_step)

    @property
    def is_exceeded(self) -> bool:
        """True if step limit has been reached."""
        return self._current_step >= self.max_steps

    @property
    def last_result(self) -> Optional[Any]:
        """Last partial result (preserved when limit hit)."""
        return self._last_result

    @property
    def policy_type(self) -> str:
        """RuntimePolicy protocol: policy type identifier."""
        return "step_limit"

    def check(self, context: PolicyContext) -> PolicyDecision:
        """RuntimePolicy protocol: check if more steps are allowed.

        Uses internal step counter (not context.step_count) since
        this guard tracks its own state across step() calls.

        Args:
            context: PolicyContext (step_count field unused)

        Returns:
            PolicyDecision allowing or denying the next step
        """
        if self._current_step >= self.max_steps:
            return PolicyDecision(
                allowed=False,
                policy_type=self.policy_type,
                reason=f"Step limit reached: {self._current_step}/{self.max_steps}",
                partial_result=self._last_result,
            )
        return PolicyDecision(allowed=True, policy_type=self.policy_type)

    def reset(self) -> None:
        """Reset step counter and partial result."""
        self._current_step = 0
        self._last_result = None
        logger.info("[VERONICA_AGENT] Step counter reset")
