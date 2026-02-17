"""VERONICA Circuit Breaker - Automatic failure isolation for LLM calls.

Tracks consecutive failures and opens the circuit when the threshold
is exceeded. After a recovery timeout, allows a single test request
(half-open state). Implements the RuntimePolicy protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional
import logging
import time

from veronica_core.runtime_policy import PolicyContext, PolicyDecision

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "CLOSED"  # Normal operation, requests allowed
    OPEN = "OPEN"  # Failure threshold exceeded, requests blocked
    HALF_OPEN = "HALF_OPEN"  # Recovery timeout elapsed, testing one request


@dataclass
class CircuitBreaker:
    """Circuit breaker for LLM call failure isolation.

    When consecutive failures reach the threshold, the circuit opens
    and all subsequent checks are denied. After recovery_timeout seconds,
    the circuit transitions to half-open and allows one test request.
    A successful test closes the circuit; a failure reopens it.

    Example:
        breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
        decision = breaker.check(PolicyContext())
        if decision.allowed:
            try:
                result = call_llm()
                breaker.record_success()
            except Exception:
                breaker.record_failure()
    """

    failure_threshold: int = 5
    recovery_timeout: float = 60.0

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: Optional[float] = field(default=None, init=False)
    _success_count: int = field(default=0, init=False)

    @property
    def policy_type(self) -> str:
        """RuntimePolicy protocol: policy type identifier."""
        return "circuit_breaker"

    @property
    def state(self) -> CircuitState:
        """Current circuit state (may auto-transition to HALF_OPEN)."""
        self._maybe_half_open()
        return self._state

    def check(self, context: PolicyContext) -> PolicyDecision:
        """RuntimePolicy protocol: check if circuit allows the operation.

        Args:
            context: PolicyContext (fields unused)

        Returns:
            PolicyDecision allowing (CLOSED/HALF_OPEN) or denying (OPEN)
        """
        self._maybe_half_open()

        if self._state == CircuitState.OPEN:
            return PolicyDecision(
                allowed=False,
                policy_type=self.policy_type,
                reason=(
                    f"Circuit OPEN: "
                    f"{self._failure_count} consecutive failures"
                ),
            )

        return PolicyDecision(allowed=True, policy_type=self.policy_type)

    def record_success(self) -> None:
        """Record a successful operation.

        Closes the circuit if currently half-open.
        Resets consecutive failure counter.
        """
        self._success_count += 1

        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            logger.info("[VERONICA_CIRCUIT] Circuit closed after successful test")
        elif self._state == CircuitState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed operation.

        Increments failure counter. Opens the circuit if threshold
        is reached. Reopens from half-open on failure.
        """
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning(
                "[VERONICA_CIRCUIT] Circuit reopened after failed test"
            )
        elif self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning(
                f"[VERONICA_CIRCUIT] Circuit opened: "
                f"{self._failure_count} consecutive failures"
            )

    def _maybe_half_open(self) -> None:
        """Transition from OPEN to HALF_OPEN if recovery timeout elapsed."""
        if (
            self._state == CircuitState.OPEN
            and self._last_failure_time is not None
            and time.time() - self._last_failure_time >= self.recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            logger.info(
                "[VERONICA_CIRCUIT] Circuit half-open, allowing test request"
            )

    def reset(self) -> None:
        """Reset circuit to CLOSED state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = None
        self._success_count = 0
        logger.info("[VERONICA_CIRCUIT] Circuit reset")

    @property
    def failure_count(self) -> int:
        """Consecutive failure count."""
        return self._failure_count

    @property
    def success_count(self) -> int:
        """Total success count."""
        return self._success_count

    def to_dict(self) -> Dict:
        """Serialize circuit breaker state."""
        return {
            "state": self._state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "last_failure_time": self._last_failure_time,
            "success_count": self._success_count,
        }
