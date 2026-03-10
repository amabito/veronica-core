"""VERONICA Circuit Breaker - Automatic failure isolation for LLM calls.

Tracks consecutive failures and opens the circuit when the threshold
is exceeded. After a recovery timeout, allows a single test request
(half-open state). Implements the RuntimePolicy protocol.
"""
# nogil-audited: 2026-03-08
# Findings:
#   - All shared mutable state (_state, _failure_count, _last_failure_time,
#     _success_count, _half_open_in_flight, _owner_id) is accessed exclusively
#     inside ``with self._lock:`` blocks. No changes needed.
#   - failure_predicate is evaluated OUTSIDE the lock (documented in
#     record_failure() docstring). The predicate must only inspect the
#     exception object -- accessing CircuitBreaker state from inside a predicate
#     would deadlock. This design is nogil-safe because the predicate itself is
#     stateless relative to the breaker.

from __future__ import annotations

__all__ = [
    "CircuitState",
    "CircuitBreaker",
    "FailurePredicate",
    "ignore_exception_types",
    "count_exception_types",
    "ignore_status_codes",
]

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional
import logging
import threading
import time

from veronica_core.runtime_policy import PolicyContext, PolicyDecision

# Type alias: predicate receives an exception and returns True if it should
# count as a circuit-breaker failure, False to ignore.
FailurePredicate = Callable[[BaseException], bool]

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
    failure_predicate: Optional[FailurePredicate] = None

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)

    def __post_init__(self) -> None:
        if isinstance(self.failure_threshold, bool) or not isinstance(self.failure_threshold, int) or math.isnan(
            float(self.failure_threshold)
        ):
            raise ValueError(
                f"failure_threshold must be a finite integer >= 1, got {self.failure_threshold}"
            )
        if self.failure_threshold < 1:
            raise ValueError(
                f"failure_threshold must be >= 1, got {self.failure_threshold}"
            )
        if not math.isfinite(self.recovery_timeout) or self.recovery_timeout < 0:
            raise ValueError(
                f"recovery_timeout must be a finite float >= 0, got {self.recovery_timeout}"
            )

    _failure_count: int = field(default=0, init=False)
    _last_failure_time: Optional[float] = field(default=None, init=False)
    _success_count: int = field(default=0, init=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _owner_id: Optional[str] = field(default=None, init=False, repr=False)
    _half_open_in_flight: int = field(default=0, init=False)

    @property
    def policy_type(self) -> str:
        """RuntimePolicy protocol: policy type identifier."""
        return "circuit_breaker"

    def bind_to_context(self, ctx_id: str) -> None:
        """Bind this CircuitBreaker to a specific ExecutionContext.

        Prevents accidental sharing of a single CircuitBreaker across
        multiple independent contexts, which would corrupt failure counts.

        Args:
            ctx_id: The chain_id of the ExecutionContext binding this breaker.

        Raises:
            RuntimeError: If already bound to a different ctx_id.
        """
        with self._lock:
            if self._owner_id is None:
                self._owner_id = ctx_id
            elif self._owner_id != ctx_id:
                raise RuntimeError(
                    "CircuitBreaker instance is being shared across contexts; "
                    "create a new one per ExecutionContext."
                )

    @property
    def state(self) -> CircuitState:
        """Current circuit state (may auto-transition to HALF_OPEN)."""
        with self._lock:
            self._maybe_half_open_locked()
            return self._state

    def check(self, context: PolicyContext) -> PolicyDecision:
        """RuntimePolicy protocol: check if circuit allows the operation.

        Args:
            context: PolicyContext (fields unused)

        Returns:
            PolicyDecision allowing (CLOSED/HALF_OPEN) or denying (OPEN)
        """
        with self._lock:
            self._maybe_half_open_locked()

            if self._state == CircuitState.OPEN:
                return PolicyDecision(
                    allowed=False,
                    policy_type=self.policy_type,
                    reason=(
                        f"Circuit OPEN: {self._failure_count} consecutive failures"
                    ),
                )

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_in_flight > 0:
                    return PolicyDecision(
                        allowed=False,
                        policy_type=self.policy_type,
                        reason="Circuit HALF_OPEN: test request already in flight",
                    )
                self._half_open_in_flight += 1

        return PolicyDecision(allowed=True, policy_type=self.policy_type)

    def record_success(self) -> None:
        """Record a successful operation.

        Closes the circuit if currently half-open.
        Resets consecutive failure counter.
        """
        with self._lock:
            self._success_count += 1
            self._half_open_in_flight = 0

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                logger.info("[VERONICA_CIRCUIT] Circuit closed after successful test")
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self, *, error: Optional[BaseException] = None) -> bool:
        """Record a failed operation.

        If a ``failure_predicate`` is configured and *error* is provided, the
        predicate is evaluated first. If it returns ``False``, the failure is
        ignored (not counted toward the threshold).

        When *error* is ``None``, the failure always counts regardless of the
        predicate (backward compatible with callers that have no exception).

        M4 WARNING: ``failure_predicate`` is evaluated OUTSIDE ``self._lock``.
        The predicate MUST NOT call any method on this CircuitBreaker instance
        (e.g. ``check()``, ``record_success()``, ``state``) or access ``self._lock``
        in any way — doing so will deadlock because ``record_failure()`` re-acquires
        ``self._lock`` after predicate evaluation. The predicate should only inspect
        the exception itself (its type, message, or attributes).

        Args:
            error: The exception that caused the failure.  When ``None``,
                the failure is always counted.

        Returns:
            ``True`` if the failure was counted, ``False`` if filtered.
        """
        if error is not None and self.failure_predicate is not None:
            try:
                if not self.failure_predicate(error):
                    logger.debug(
                        "[VERONICA_CIRCUIT] Failure filtered by predicate: %s",
                        type(error).__name__,
                    )
                    # Release the HALF_OPEN in-flight slot so the circuit can
                    # allow the next test request.  Without this, a filtered
                    # failure leaves the slot permanently occupied and the
                    # circuit can never recover.
                    #
                    # Note: there is a benign TOCTOU window between the predicate
                    # evaluation above (outside the lock) and the state check below
                    # (inside the lock).  In the worst case, the circuit transitions
                    # from HALF_OPEN to OPEN between the two reads.  The only
                    # consequence is a redundant "_half_open_in_flight = 0" reset
                    # on an already-OPEN circuit, which is harmless: the slot counter
                    # is always reset on the OPEN→* transitions anyway.
                    with self._lock:
                        if self._state == CircuitState.HALF_OPEN:
                            self._half_open_in_flight = 0
                    return False
            except Exception:
                logger.warning(
                    "[VERONICA_CIRCUIT] failure_predicate raised; "
                    "counting failure as fail-safe"
                )

        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            self._half_open_in_flight = 0

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning("[VERONICA_CIRCUIT] Circuit reopened after failed test")
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "[VERONICA_CIRCUIT] Circuit opened: %d consecutive failures",
                    self._failure_count,
                )
        return True

    def _maybe_half_open_locked(self) -> None:
        """Transition from OPEN to HALF_OPEN if recovery timeout elapsed.

        Must be called with self._lock held.
        """
        if (
            self._state == CircuitState.OPEN
            and self._last_failure_time is not None
            and time.monotonic() - self._last_failure_time >= self.recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            logger.info("[VERONICA_CIRCUIT] Circuit half-open, allowing test request")

    def reset(self) -> None:
        """Reset circuit to CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
            self._success_count = 0
            self._half_open_in_flight = 0
        logger.info("[VERONICA_CIRCUIT] Circuit reset")

    @property
    def failure_count(self) -> int:
        """Consecutive failure count."""
        with self._lock:
            return self._failure_count

    @property
    def success_count(self) -> int:
        """Total success count."""
        with self._lock:
            return self._success_count

    def to_dict(self) -> Dict:
        """Serialize circuit breaker state."""
        with self._lock:
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout": self.recovery_timeout,
                "last_failure_time": self._last_failure_time,
                "success_count": self._success_count,
            }


# ---------------------------------------------------------------------------
# Built-in failure predicate factories
# ---------------------------------------------------------------------------


def ignore_exception_types(
    *exception_types: type,
) -> FailurePredicate:
    """Create a predicate that ignores (does not count) the given exception types.

    Useful for filtering out user-caused errors that should not trip the
    circuit breaker (e.g. bad prompts, invalid parameters).

    Example::

        breaker = CircuitBreaker(
            failure_predicate=ignore_exception_types(ValueError, BadRequestError),
        )
    """

    def predicate(error: BaseException) -> bool:
        return not isinstance(error, exception_types)

    return predicate


def count_exception_types(
    *exception_types: type,
) -> FailurePredicate:
    """Create a predicate that only counts the given exception types as failures.

    All other exception types are ignored.  Useful for whitelisting: only
    provider-side errors (500s, timeouts) trip the breaker.

    Example::

        breaker = CircuitBreaker(
            failure_predicate=count_exception_types(TimeoutError, ServerError),
        )
    """

    def predicate(error: BaseException) -> bool:
        return isinstance(error, exception_types)

    return predicate


def ignore_status_codes(*codes: int) -> FailurePredicate:
    """Create a predicate that ignores HTTP errors with the given status codes.

    Inspects the exception for a ``status_code`` attribute or a
    ``response.status_code`` attribute.  Non-HTTP exceptions (those without
    either attribute) always count as failures.

    Example::

        breaker = CircuitBreaker(
            failure_predicate=ignore_status_codes(400, 404, 422),
        )
    """
    code_set = frozenset(codes)

    def predicate(error: BaseException) -> bool:
        status = getattr(error, "status_code", None)
        if status is None:
            resp = getattr(error, "response", None)
            if resp is not None:
                status = getattr(resp, "status_code", None)
        if status is not None and status in code_set:
            return False
        return True

    return predicate
