"""MemoryGovernor -- thread-safe orchestrator for memory governance hooks.

The governor evaluates a chain of MemoryGovernanceHook instances in order
and aggregates their verdicts according to fail-closed semantics by default:

- No hooks + fail_closed=True  -> DENY
- No hooks + fail_closed=False -> ALLOW
- First DENY from any hook     -> stops evaluation, returns DENY
- Hook raises                  -> treated as DENY (fail-closed)
- QUARANTINE / DEGRADE         -> worst verdict propagates (QUARANTINE > DEGRADE > ALLOW)

Thread safety: add_hook() and evaluate() are protected by a non-reentrant lock.
Hooks MUST NOT call back into the same MemoryGovernor instance from within
before_op() or after_op() -- doing so will deadlock.
notify_after() never raises regardless of hook errors.
"""

from __future__ import annotations

__all__ = ["MemoryGovernor"]

import logging
import threading
from typing import Any

from veronica_core.memory.hooks import MemoryGovernanceHook
from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
)

logger = logging.getLogger(__name__)

_MAX_HOOKS = 100

# Verdict severity ordering -- higher index wins when merging non-DENY verdicts.
_VERDICT_RANK: dict[GovernanceVerdict, int] = {
    GovernanceVerdict.ALLOW: 0,
    GovernanceVerdict.DEGRADE: 1,
    GovernanceVerdict.QUARANTINE: 2,
    # DENY is handled separately (short-circuit) and not ranked here.
}


class MemoryGovernor:
    """Orchestrates memory governance hooks in a thread-safe pipeline.

    Usage::

        governor = MemoryGovernor(fail_closed=True)
        governor.add_hook(MyPolicyHook())

        op = MemoryOperation(action=MemoryAction.WRITE, agent_id="agent-1")
        decision = governor.evaluate(op)
        if decision.denied:
            raise PermissionError(decision.reason)

    Hooks are evaluated in registration order.  The first DENY terminates
    evaluation.  QUARANTINE and DEGRADE accumulate (worst verdict wins).
    """

    def __init__(
        self,
        hooks: list[MemoryGovernanceHook] | None = None,
        fail_closed: bool = True,
    ) -> None:
        """Create a MemoryGovernor.

        Args:
            hooks: Initial list of hooks (copied, not stored directly).
            fail_closed: If True, zero-hook evaluations return DENY.
                         If False, zero-hook evaluations return ALLOW.
        """
        self._hooks: list[MemoryGovernanceHook] = list(hooks or [])
        self._fail_closed = fail_closed
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_hook(self, hook: MemoryGovernanceHook) -> None:
        """Register a governance hook.

        Args:
            hook: Any object satisfying the MemoryGovernanceHook protocol.

        Raises:
            RuntimeError: When the hook count would exceed 100.
        """
        with self._lock:
            if len(self._hooks) >= _MAX_HOOKS:
                raise RuntimeError(
                    f"MemoryGovernor hook count capped at {_MAX_HOOKS}; "
                    "cannot add more hooks"
                )
            self._hooks.append(hook)

    def evaluate(
        self,
        operation: MemoryOperation,
        context: MemoryPolicyContext | None = None,
    ) -> MemoryGovernanceDecision:
        """Evaluate all hooks and return an aggregated governance decision.

        If context is None a minimal default context is constructed for the
        operation so hooks always receive a fully-typed context object.

        Args:
            operation: The memory operation to evaluate.
            context: Optional chain context.  Created from operation if None.

        Returns:
            MemoryGovernanceDecision with the aggregated verdict.
        """
        if context is None:
            context = MemoryPolicyContext(operation=operation)

        with self._lock:
            hooks_snapshot = tuple(self._hooks)

        if not hooks_snapshot:
            verdict = (
                GovernanceVerdict.DENY if self._fail_closed else GovernanceVerdict.ALLOW
            )
            reason = (
                "no hooks registered (fail-closed)"
                if self._fail_closed
                else "no hooks registered (fail-open)"
            )
            return MemoryGovernanceDecision(
                verdict=verdict,
                reason=reason,
                policy_id="governor",
                operation=operation,
            )

        # Accumulate worst non-DENY verdict across all hooks.
        accumulated_verdict = GovernanceVerdict.ALLOW
        accumulated_reason = ""
        accumulated_policy_id = "governor"

        for hook in hooks_snapshot:
            try:
                decision = hook.before_op(operation, context)
                if decision is None:
                    raise TypeError(
                        f"hook {type(hook).__name__}.before_op() returned None"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[memory.governor] hook %s raised during before_op: %s",
                    type(hook).__name__,
                    exc,
                )
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=f"hook error: {type(exc).__name__}",
                    policy_id=type(hook).__name__,
                    operation=operation,
                )

            if decision.verdict is GovernanceVerdict.DENY:
                # Fail-closed: first DENY stops evaluation immediately.
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=decision.reason,
                    policy_id=decision.policy_id,
                    operation=operation,
                    audit_metadata=dict(decision.audit_metadata),
                )

            # Track worst non-DENY verdict.  Fail-closed: unknown verdicts
            # are treated as DENY to prevent silent degradation.
            try:
                hook_rank = _VERDICT_RANK[decision.verdict]
            except KeyError:
                logger.error(
                    "[memory.governor] hook %s returned unknown verdict %r; "
                    "failing closed (DENY)",
                    type(hook).__name__,
                    decision.verdict,
                )
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=f"unknown verdict: {decision.verdict!r}",
                    policy_id=type(hook).__name__,
                    operation=operation,
                )
            current_rank = _VERDICT_RANK[accumulated_verdict]
            if hook_rank > current_rank:
                accumulated_verdict = decision.verdict
                accumulated_reason = decision.reason
                accumulated_policy_id = decision.policy_id

        return MemoryGovernanceDecision(
            verdict=accumulated_verdict,
            reason=accumulated_reason,
            policy_id=accumulated_policy_id,
            operation=operation,
        )

    def notify_after(
        self,
        operation: MemoryOperation,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """Call after_op() on all registered hooks.

        Errors from individual hooks are logged and swallowed; this method
        never raises regardless of hook behavior.

        Args:
            operation: The memory operation that was evaluated.
            decision: The governance decision that was applied.
            result: Return value from the memory operation, if any.
            error: Exception raised by the memory operation, if any.
        """
        with self._lock:
            hooks_snapshot = tuple(self._hooks)

        for hook in hooks_snapshot:
            try:
                hook.after_op(operation, decision, result=result, error=error)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[memory.governor] hook %s raised during after_op: %s",
                    type(hook).__name__,
                    exc,
                )

    @property
    def hook_count(self) -> int:
        """Number of registered hooks."""
        with self._lock:
            return len(self._hooks)
