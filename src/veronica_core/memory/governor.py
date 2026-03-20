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
from veronica_core.memory.message_governance import MessageGovernanceHook
from veronica_core.memory.types import (
    DegradeDirective,
    GovernanceVerdict,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
    MessageContext,
    ThreatContext,
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

# Bug #8: mode merge -- deterministic, commutative strictness order.
# Higher index = more restrictive; unknown modes treated as most restrictive.
_MODE_RANK: dict[str, int] = {
    "compact": 0,
    "truncate": 1,
    "redact": 2,
    "downscope": 3,
}


def _merge_limit(a: int, b: int) -> int:
    """Return the stricter (smaller positive) of two limit values.

    Semantics: 0 means "no limit" (unlimited). A literal limit of exactly
    zero tokens/bytes is not representable via this function -- callers that
    need to express "allow nothing" should use a positive sentinel (e.g. 1)
    or a separate boolean flag, not a zero limit value.

    If both are positive, the smaller wins (stricter).
    If one is 0, the other (positive) value is the effective limit.
    If both are 0, the result is 0 (no limit from either side).
    """
    if a == 0:
        return b
    if b == 0:
        return a
    return min(a, b)


def _merge_directives(
    existing: DegradeDirective | None,
    new: DegradeDirective | None,
) -> DegradeDirective | None:
    """Merge two DegradeDirective instances, returning a combined directive.

    Merging rules per field type:
    - bool:  OR (True wins)
    - int:   stricter of non-zero values (0 = no limit; min of positives)
    - float: min of non-1.0 values (stricter wins for ratios)
    - str:   new value if non-empty, else existing
    - tuple: union (sorted for determinism)
    """
    if new is None:
        return existing
    if existing is None:
        return new
    # Bug #8: mode -- commutative merge by strictness rank (higher = more restrictive).
    # If both are non-empty, pick the stricter one; unknown modes rank highest (most
    # restrictive). If one is empty, the non-empty value wins.
    if existing.mode and new.mode:
        existing_rank = _MODE_RANK.get(existing.mode, len(_MODE_RANK))
        new_rank = _MODE_RANK.get(new.mode, len(_MODE_RANK))
        merged_mode = existing.mode if existing_rank >= new_rank else new.mode
    else:
        merged_mode = existing.mode or new.mode

    # Bug #8: namespace_downscoped_to -- commutative merge.
    # If both are non-empty, prefer the longer (more specific) path; that typically
    # restricts access to a narrower subtree. Tie-break lexicographically for full
    # determinism regardless of registration order.
    if existing.namespace_downscoped_to and new.namespace_downscoped_to:
        a, b = existing.namespace_downscoped_to, new.namespace_downscoped_to
        if len(a) != len(b):
            merged_ns = a if len(a) > len(b) else b
        else:
            merged_ns = min(a, b)  # lexicographic tie-break
    else:
        merged_ns = existing.namespace_downscoped_to or new.namespace_downscoped_to

    return DegradeDirective(
        mode=merged_mode,
        max_packet_tokens=_merge_limit(
            existing.max_packet_tokens, new.max_packet_tokens
        ),
        allowed_provenance=tuple(
            sorted(set(existing.allowed_provenance) | set(new.allowed_provenance))
        ),
        verified_only=existing.verified_only or new.verified_only,
        summary_required=existing.summary_required or new.summary_required,
        raw_replay_blocked=existing.raw_replay_blocked or new.raw_replay_blocked,
        namespace_downscoped_to=merged_ns,
        redacted_fields=tuple(
            sorted(set(existing.redacted_fields) | set(new.redacted_fields))
        ),
        max_content_size_bytes=_merge_limit(
            existing.max_content_size_bytes, new.max_content_size_bytes
        ),
    )


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
        self._message_hooks: list[MessageGovernanceHook] = []
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

    def add_message_hook(self, hook: MessageGovernanceHook) -> None:
        """Register a message governance hook.

        Message hooks are evaluated by evaluate_message() in registration order.

        Args:
            hook: Any object satisfying the MessageGovernanceHook protocol.

        Raises:
            RuntimeError: When the hook count would exceed 100.
        """
        with self._lock:
            if len(self._message_hooks) >= _MAX_HOOKS:
                raise RuntimeError(
                    f"MemoryGovernor message hook count capped at {_MAX_HOOKS}; "
                    "cannot add more message hooks"
                )
            self._message_hooks.append(hook)

    def evaluate_message(self, context: MessageContext) -> MemoryGovernanceDecision:
        """Evaluate all message hooks and return an aggregated governance decision.

        Hooks are evaluated in registration order. A DENY verdict from any
        hook stops before_message evaluation immediately (fail-closed).
        DEGRADE verdicts are accumulated; only DENY short-circuits.
        after_message is called on every registered hook regardless of the
        final verdict.

        DegradeDirective instances from multiple DEGRADE hooks are merged using
        the same merge semantics as evaluate() (stricter fields win).

        Args:
            context: The message context to evaluate.

        Returns:
            MemoryGovernanceDecision with the aggregated verdict.
        """
        with self._lock:
            hooks_snapshot = tuple(self._message_hooks)

        if not hooks_snapshot:
            verdict = (
                GovernanceVerdict.DENY if self._fail_closed else GovernanceVerdict.ALLOW
            )
            reason = (
                "no message hooks registered (fail-closed)"
                if self._fail_closed
                else "no message hooks registered (fail-open)"
            )
            return MemoryGovernanceDecision(
                verdict=verdict,
                reason=reason,
                policy_id="governor",
            )

        accumulated_verdict = GovernanceVerdict.ALLOW
        accumulated_reason = ""
        accumulated_policy_id = "governor"
        accumulated_directive: DegradeDirective | None = None
        accumulated_threat: ThreatContext | None = None
        final_decision: MemoryGovernanceDecision | None = None

        for hook in hooks_snapshot:
            try:
                decision = hook.before_message(context)
                if decision is None:
                    raise TypeError(
                        f"hook {type(hook).__name__}.before_message() returned None"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[memory.governor] message hook %s raised during before_message: %s",
                    type(hook).__name__,
                    exc,
                )
                final_decision = MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason="hook error: hook raised unexpectedly",
                    policy_id=type(hook).__name__,
                )
                break

            if decision.verdict is GovernanceVerdict.DENY:
                final_decision = MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=decision.reason,
                    policy_id=decision.policy_id,
                    audit_metadata=dict(decision.audit_metadata),
                )
                break

            try:
                hook_rank = _VERDICT_RANK[decision.verdict]
            except KeyError:
                logger.error(
                    "[memory.governor] message hook %s returned unknown verdict %r; "
                    "failing closed (DENY)",
                    type(hook).__name__,
                    decision.verdict,
                )
                final_decision = MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=f"unknown verdict: {decision.verdict!r}",
                    policy_id=type(hook).__name__,
                )
                break

            current_rank = _VERDICT_RANK[accumulated_verdict]
            if hook_rank > current_rank:
                accumulated_verdict = decision.verdict
                accumulated_reason = decision.reason
                accumulated_policy_id = decision.policy_id
                accumulated_threat = decision.threat_context

            if (
                decision.verdict is GovernanceVerdict.DEGRADE
                and decision.degrade_directive is not None
            ):
                accumulated_directive = _merge_directives(
                    accumulated_directive, decision.degrade_directive
                )

        # Call after_message on all hooks (fire-and-forget, never raises).
        resolved = final_decision or MemoryGovernanceDecision(
            verdict=accumulated_verdict,
            reason=accumulated_reason,
            policy_id=accumulated_policy_id,
            degrade_directive=(
                accumulated_directive
                if accumulated_verdict
                in (
                    GovernanceVerdict.DEGRADE,
                    GovernanceVerdict.QUARANTINE,
                )
                else None
            ),
            threat_context=(
                accumulated_threat
                if accumulated_verdict is not GovernanceVerdict.ALLOW
                else None
            ),
        )
        for hook in hooks_snapshot:
            try:
                hook.after_message(context, resolved)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[memory.governor] message hook %s raised during after_message: %s",
                    type(hook).__name__,
                    exc,
                )
        return resolved

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
        accumulated_directive: DegradeDirective | None = None
        accumulated_threat: ThreatContext | None = None

        # Bug #10: use a sentinel so early exits (exception, DENY, unknown verdict)
        # can still call notify_after before returning.
        early_exit_decision: MemoryGovernanceDecision | None = None

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
                early_exit_decision = MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason="hook error: hook raised unexpectedly",
                    policy_id=type(hook).__name__,
                    operation=operation,
                )
                break

            if decision.verdict is GovernanceVerdict.DENY:
                # Fail-closed: first DENY stops evaluation immediately.
                early_exit_decision = MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=decision.reason,
                    policy_id=decision.policy_id,
                    operation=operation,
                    audit_metadata=dict(decision.audit_metadata),
                )
                break

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
                early_exit_decision = MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=f"unknown verdict: {decision.verdict!r}",
                    policy_id=type(hook).__name__,
                    operation=operation,
                )
                break

            current_rank = _VERDICT_RANK[accumulated_verdict]
            if hook_rank > current_rank:
                accumulated_verdict = decision.verdict
                accumulated_reason = decision.reason
                accumulated_policy_id = decision.policy_id
                # Propagate threat_context from the worst-verdict hook.
                accumulated_threat = decision.threat_context

            # Merge DegradeDirective from every DEGRADE hook encountered.
            if (
                decision.verdict is GovernanceVerdict.DEGRADE
                and decision.degrade_directive is not None
            ):
                accumulated_directive = _merge_directives(
                    accumulated_directive, decision.degrade_directive
                )

        if early_exit_decision is not None:
            # Bug #10: notify_after must be called even on early-exit paths.
            self.notify_after(operation, early_exit_decision)
            return early_exit_decision

        # Bug #9: preserve accumulated DEGRADE directive when the final verdict
        # is QUARANTINE (a later hook may have escalated from DEGRADE). The
        # directive still carries valid content-transformation instructions.
        final_directive = (
            accumulated_directive
            if accumulated_verdict
            in (
                GovernanceVerdict.DEGRADE,
                GovernanceVerdict.QUARANTINE,
            )
            else None
        )
        final_threat = (
            accumulated_threat
            if accumulated_verdict is not GovernanceVerdict.ALLOW
            else None
        )
        return MemoryGovernanceDecision(
            verdict=accumulated_verdict,
            reason=accumulated_reason,
            policy_id=accumulated_policy_id,
            operation=operation,
            degrade_directive=final_directive,
            threat_context=final_threat,
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
        """Number of registered memory operation hooks."""
        with self._lock:
            return len(self._hooks)

    @property
    def message_hook_count(self) -> int:
        """Number of registered message hooks."""
        with self._lock:
            return len(self._message_hooks)
