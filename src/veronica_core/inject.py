"""veronica_core.inject — Decorator-based execution boundary injection.

Public API:
    veronica_guard  — decorator that wraps a callable in a policy-enforced boundary
    GuardConfig     — dataclass for documentation/IDE autocomplete (unused at runtime)
    VeronicaHalt    — exception raised when a policy denies execution
    is_guard_active — returns True when called from inside a guard boundary
"""
from __future__ import annotations

import functools
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Optional

from veronica_core.runtime_policy import PolicyDecision

__all__ = ["veronica_guard", "GuardConfig", "VeronicaHalt", "is_guard_active"]

# ContextVar: set to True while inside a guard-wrapped call.
# Lets future transparent injection detect an active guard without inspecting
# the call stack.
_guard_active: ContextVar[bool] = ContextVar("veronica_guard_active", default=False)


def is_guard_active() -> bool:
    """Return True if the current call is executing inside a veronica_guard boundary."""
    return _guard_active.get()


class VeronicaHalt(RuntimeError):
    """Raised when veronica_guard denies execution due to a policy decision."""

    def __init__(self, reason: str, decision: PolicyDecision) -> None:
        super().__init__(reason)
        self.reason = reason
        self.decision = decision


@dataclass
class GuardConfig:
    """Configuration mirror for veronica_guard parameters.

    Exists for documentation and IDE autocomplete. veronica_guard accepts
    the same parameters directly as keyword arguments.

    Attributes:
        max_cost_usd: Hard cost ceiling passed to BudgetEnforcer.
        max_steps: Step count ceiling passed to AgentStepGuard.
        max_retries_total: Retry ceiling passed to RetryContainer.
        timeout_ms: Reserved for future enforcement. Currently unused.
    """

    max_cost_usd: float = 1.0
    max_steps: int = 25
    max_retries_total: int = 3
    timeout_ms: Optional[float] = None


def veronica_guard(
    max_cost_usd: float = 1.0,
    max_steps: int = 25,
    max_retries_total: int = 3,
    timeout_ms: Optional[float] = None,  # reserved for future enforcement
    return_decision: bool = False,
) -> Callable:
    """Decorator that wraps a callable inside an AIcontainer execution boundary.

    Creates one AIcontainer per decorated function (shared across invocations).
    The container holds BudgetEnforcer, RetryContainer, and AgentStepGuard
    configured from the decorator arguments.

    Args:
        max_cost_usd: Hard cost ceiling. Passed to BudgetEnforcer(limit_usd=...).
        max_steps: Step count ceiling. Passed to AgentStepGuard(max_steps=...).
        max_retries_total: Retry ceiling. Passed to RetryContainer(max_retries=...).
        timeout_ms: Reserved. Currently unused.
        return_decision: If True, return PolicyDecision on denial instead of raising.

    Returns:
        A decorator that enforces the configured policy on the wrapped function.

    Raises:
        VeronicaHalt: When a policy denies execution and return_decision is False.

    Example::

        @veronica_guard(max_cost_usd=1.0, max_steps=20)
        def call_llm(prompt: str) -> str:
            return llm.complete(prompt)

        # Raises VeronicaHalt if policies deny.
        result = call_llm("Hello")
    """
    from veronica_core import BudgetEnforcer, RetryContainer, AgentStepGuard
    from veronica_core.container import AIcontainer

    container = AIcontainer(
        budget=BudgetEnforcer(limit_usd=max_cost_usd),
        retry=RetryContainer(max_retries=max_retries_total),
        step_guard=AgentStepGuard(max_steps=max_steps),
    )

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            decision = container.check()
            if not decision.allowed:
                if return_decision:
                    return decision
                raise VeronicaHalt(decision.reason, decision)

            token = _guard_active.set(True)
            try:
                return func(*args, **kwargs)
            finally:
                _guard_active.reset(token)

        # Expose container for testing and introspection.
        wrapper._container = container  # type: ignore[attr-defined]
        return wrapper

    return decorator
