"""Memory governance hook protocols and default implementations.

Hooks are the extension points for the MemoryGovernor.  Any object that
implements MemoryGovernanceHook (structurally, via Protocol) can be registered.

Two built-in implementations are provided:
- DefaultMemoryGovernanceHook: allows all operations (fail-open default)
- DenyAllMemoryGovernanceHook: denies all operations (fail-closed default)
"""

from __future__ import annotations

__all__ = [
    "MemoryGovernanceHook",
    "DefaultMemoryGovernanceHook",
    "DenyAllMemoryGovernanceHook",
]

import logging
from typing import Any, Protocol, runtime_checkable

from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class MemoryGovernanceHook(Protocol):
    """Protocol for memory governance extension points.

    Implementors intercept memory operations before and after they execute.
    before_op() returns a governance decision; after_op() is fire-and-forget.

    Any class that structurally provides both methods satisfies this protocol.
    """

    def before_op(
        self,
        operation: MemoryOperation,
        context: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        """Evaluate the operation before it executes.

        Args:
            operation: The memory operation being requested.
            context: Ambient chain context, may be None.

        Returns:
            MemoryGovernanceDecision -- verdict determines whether to proceed.
        """
        ...

    def after_op(
        self,
        operation: MemoryOperation,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """Notification after the operation completes or fails.

        This method must not raise.  Implementations should swallow errors
        internally and log them if needed.

        Args:
            operation: The memory operation that was evaluated.
            decision: The governance decision that was applied.
            result: Return value from the memory operation, if any.
            error: Exception raised by the operation, if any.
        """
        ...


class DefaultMemoryGovernanceHook:
    """Fail-open hook -- allows all operations.

    Suitable as a no-op placeholder or for development environments.
    after_op() logs any error at WARNING level without re-raising.
    """

    def before_op(
        self,
        operation: MemoryOperation,
        context: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        """Allow the operation unconditionally."""
        return MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="default allow",
            policy_id="default",
            operation=operation,
        )

    def after_op(
        self,
        operation: MemoryOperation,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """Log errors at WARNING; never raises."""
        if error is not None:
            logger.warning(
                "[memory.hooks] after_op error for action=%s resource=%s: %s",
                operation.action.value,
                operation.resource_id,
                error,
            )


class DenyAllMemoryGovernanceHook:
    """Fail-closed hook -- denies all operations.

    Use as the default governor hook when no other hooks are registered
    and fail-closed semantics are required.
    """

    def before_op(
        self,
        operation: MemoryOperation,
        context: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        """Deny the operation unconditionally."""
        return MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DENY,
            reason="deny-all policy",
            policy_id="deny_all",
            operation=operation,
        )

    def after_op(
        self,
        operation: MemoryOperation,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """No-op -- deny-all hooks do not need post-operation callbacks."""
