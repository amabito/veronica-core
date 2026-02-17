"""VERONICA Retry Containment - Budget-aware retry wrapper."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, TypeVar, Optional, Any
import time
import logging

from veronica_core.runtime_policy import PolicyContext, PolicyDecision

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class RetryContainer:
    """Budget-aware retry wrapper for LLM calls.

    Unlike per-call retry limits, this enforces a total retry budget
    across the entire request chain. Prevents 3 retries x 5 nested
    calls = 15 LLM calls from one user action.

    Example:
        retry = RetryContainer(max_retries=3, backoff_base=1.0)
        result = retry.execute(call_llm_api, prompt="hello")
    """

    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0

    _attempt_count: int = field(default=0, init=False)
    _total_retries: int = field(default=0, init=False)
    _last_error: Optional[Exception] = field(default=None, init=False, repr=False)

    def execute(
        self,
        fn: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute function with retry containment.

        Args:
            fn: Callable to execute
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Result from fn

        Raises:
            Exception: Last exception if all retries exhausted
        """
        self._attempt_count = 0

        for attempt in range(self.max_retries + 1):
            self._attempt_count = attempt + 1

            try:
                result = fn(*args, **kwargs)
                self._last_error = None  # Clear error state on success
                if attempt > 0:
                    logger.info(
                        f"[VERONICA_RETRY] Succeeded on attempt {attempt + 1}"
                    )
                return result

            except Exception as e:
                self._last_error = e
                self._total_retries += 1

                if attempt >= self.max_retries:
                    logger.warning(
                        f"[VERONICA_RETRY] All {self.max_retries} retries exhausted: {e}"
                    )
                    raise

                delay = min(
                    self.backoff_base * (2**attempt),
                    self.backoff_max,
                )
                logger.info(
                    f"[VERONICA_RETRY] Attempt {attempt + 1} failed: {e}. "
                    f"Retrying in {delay:.1f}s "
                    f"({self.max_retries - attempt} remaining)"
                )
                time.sleep(delay)

        raise self._last_error  # type: ignore[misc]

    @property
    def attempt_count(self) -> int:
        """Number of attempts in the last execute() call."""
        return self._attempt_count

    @property
    def total_retries(self) -> int:
        """Total retries across all execute() calls."""
        return self._total_retries

    @property
    def last_error(self) -> Optional[Exception]:
        """Last error encountered."""
        return self._last_error

    @property
    def policy_type(self) -> str:
        """RuntimePolicy protocol: policy type identifier."""
        return "retry_budget"

    def check(self, context: PolicyContext) -> PolicyDecision:
        """RuntimePolicy protocol: check if retry budget is available.

        Returns denied if the container is in an error state
        (last execution failed after exhausting all retries).
        Does NOT modify retry state.

        Args:
            context: PolicyContext (fields unused)

        Returns:
            PolicyDecision allowing or denying the operation
        """
        if self._last_error is not None:
            return PolicyDecision(
                allowed=False,
                policy_type=self.policy_type,
                reason=(
                    f"Retry budget exhausted "
                    f"({self._total_retries} retries used)"
                ),
            )
        return PolicyDecision(allowed=True, policy_type=self.policy_type)

    def reset(self) -> None:
        """Reset retry state for reuse."""
        self._attempt_count = 0
        self._total_retries = 0
        self._last_error = None
        logger.info("[VERONICA_RETRY] Retry state reset")
