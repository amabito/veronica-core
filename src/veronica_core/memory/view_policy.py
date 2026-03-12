"""Memory view and execution mode policy evaluator."""
from __future__ import annotations

__all__ = ["ViewPolicyEvaluator"]

from typing import Any

from veronica_core.memory.types import (
    ExecutionMode,
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
    MemoryView,
    ThreatContext,
    trust_rank as _trust_rank,
)

_POLICY_ID = "view_policy"

# Write actions -- used to distinguish read vs write requests.
_WRITE_ACTIONS: frozenset[MemoryAction] = frozenset(
    {
        MemoryAction.WRITE,
        MemoryAction.ARCHIVE,
        MemoryAction.CONSOLIDATE,
        MemoryAction.DELETE,
        MemoryAction.QUARANTINE,
    }
)


def _is_write(action: MemoryAction) -> bool:
    return action in _WRITE_ACTIONS


class ViewPolicyEvaluator:
    """Evaluates memory access based on view, trust level, and execution mode.

    View access rules (default, applied in order):
    - AGENT_PRIVATE:        owner only (agent_id must match owner_agent_id).
    - LOCAL_WORKING:        any trust level, any mode.
    - TEAM_SHARED:          provisional+ for read; trusted+ for write.
    - SESSION_STATE:        trusted+ for read; privileged for write.
                            Exception: CONSOLIDATION mode allows trusted+ write.
    - VERIFIED_ARCHIVE:     trusted+ read-only.
                            Exception: CONSOLIDATION mode allows trusted+ write.
    - PROVISIONAL_ARCHIVE:  provisional+ for read; trusted+ for write.
    - QUARANTINED:          privileged read-only.
                            Exception: AUDIT_REVIEW mode allows trusted+ read.

    Execution mode overrides (applied first, before view matrix):
    - REPLAY:        all writes -> DENY.
    - AUDIT_REVIEW:  all writes -> DENY; quarantined read allowed for trusted+.
    - SIMULATION:    writes to VERIFIED_ARCHIVE / SESSION_STATE -> DENY.
    - CONSOLIDATION: writes to VERIFIED_ARCHIVE / SESSION_STATE allowed for trusted+.
    - LIVE:          untrusted agents accessing VERIFIED_ARCHIVE -> DENY.

    Thread-safe: no mutable instance state (owner_agent_id is immutable str).
    """

    def __init__(self, owner_agent_id: str = "") -> None:
        """Create a ViewPolicyEvaluator.

        Args:
            owner_agent_id: The agent_id that owns the AGENT_PRIVATE view.
                Operations on AGENT_PRIVATE from any other agent_id are denied.
        """
        self._owner = owner_agent_id

    # ------------------------------------------------------------------
    # MemoryGovernanceHook protocol
    # ------------------------------------------------------------------

    def before_op(
        self,
        operation: MemoryOperation,
        context: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        """Evaluate view and execution-mode access for *operation*."""
        view, mode, trust = _resolve_context(context)
        is_write_op = _is_write(operation.action)

        # --- AGENT_PRIVATE: owner-only check ---
        if view is MemoryView.AGENT_PRIVATE:
            if operation.agent_id != self._owner:
                return _deny(
                    operation,
                    reason=(
                        f"AGENT_PRIVATE view: agent_id {operation.agent_id!r} "
                        f"is not the owner {self._owner!r}"
                    ),
                    effective_view=view.value,
                    effective_scope="agent_private",
                    mitigation="deny",
                )

        # --- Execution-mode overrides (order matters) ---

        # REPLAY: all writes denied
        if mode is ExecutionMode.REPLAY and is_write_op:
            return _deny(
                operation,
                reason="REPLAY mode: all write operations are denied",
                effective_view=view.value,
                effective_scope="replay_read_only",
                mitigation="deny",
            )

        # AUDIT_REVIEW: all writes denied; quarantined read allowed for trusted+
        if mode is ExecutionMode.AUDIT_REVIEW:
            if is_write_op:
                return _deny(
                    operation,
                    reason="AUDIT_REVIEW mode: all write operations are denied",
                    effective_view=view.value,
                    effective_scope="audit_read_only",
                    mitigation="deny",
                )
            # Quarantined read: trusted+ allowed in AUDIT_REVIEW, others denied
            if view is MemoryView.QUARANTINED:
                if _trust_rank(trust) >= _trust_rank("trusted"):
                    return _allow(operation, view=view, scope="audit_review_quarantined")
                return _deny(
                    operation,
                    reason=(
                        f"QUARANTINED view in AUDIT_REVIEW: trust_level {trust!r} "
                        "is below 'trusted'"
                    ),
                    effective_view=view.value,
                    effective_scope="audit_review_quarantined_denied",
                    mitigation="deny",
                )

        # SIMULATION: writes to VERIFIED_ARCHIVE / SESSION_STATE denied
        if mode is ExecutionMode.SIMULATION and is_write_op:
            if view in (MemoryView.VERIFIED_ARCHIVE, MemoryView.SESSION_STATE):
                return _deny(
                    operation,
                    reason=(
                        f"SIMULATION mode: write to {view.value} is not allowed "
                        "(no promotion to verified/session)"
                    ),
                    effective_view=view.value,
                    effective_scope="simulation_no_promotion",
                    mitigation="deny",
                )

        # LIVE mode: untrusted agents accessing VERIFIED_ARCHIVE denied
        if mode is ExecutionMode.LIVE and not is_write_op:
            if view is MemoryView.VERIFIED_ARCHIVE and _trust_rank(trust) < _trust_rank("trusted"):
                return _deny(
                    operation,
                    reason=(
                        f"LIVE mode: untrusted agent cannot read VERIFIED_ARCHIVE "
                        f"(trust_level={trust!r})"
                    ),
                    effective_view=view.value,
                    effective_scope="live_verified_denied",
                    mitigation="deny",
                )

        # --- View access matrix ---
        verdict = _check_view_access(view, mode, trust, is_write_op, self._owner, operation)
        return verdict

    def after_op(
        self,
        operation: MemoryOperation,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """No-op -- view policy has no post-operation side effects."""


# ------------------------------------------------------------------
# View access matrix
# ------------------------------------------------------------------


def _check_view_access(
    view: MemoryView,
    mode: ExecutionMode,
    trust: str,
    is_write_op: bool,
    owner: str,
    operation: MemoryOperation,
) -> MemoryGovernanceDecision:
    """Apply the default view access matrix and return a decision."""
    rank = _trust_rank(trust)

    if view is MemoryView.AGENT_PRIVATE:
        # Owner check already done above; if we reach here the agent_id matched.
        return _allow(operation, view=view, scope="agent_private_owner")

    if view is MemoryView.LOCAL_WORKING:
        # Any trust level allowed for both read and write.
        return _allow(operation, view=view, scope="local_working")

    if view is MemoryView.TEAM_SHARED:
        if is_write_op and rank < _trust_rank("trusted"):
            return _deny(
                operation,
                reason=(
                    f"TEAM_SHARED write requires 'trusted' trust level; "
                    f"got {trust!r}"
                ),
                effective_view=view.value,
                effective_scope="team_shared_write_denied",
                mitigation="deny",
            )
        if not is_write_op and rank < _trust_rank("provisional"):
            return _deny(
                operation,
                reason=(
                    f"TEAM_SHARED read requires 'provisional' trust level; "
                    f"got {trust!r}"
                ),
                effective_view=view.value,
                effective_scope="team_shared_read_denied",
                mitigation="deny",
            )
        return _allow(operation, view=view, scope="team_shared")

    if view is MemoryView.SESSION_STATE:
        if is_write_op:
            required = "trusted" if mode is ExecutionMode.CONSOLIDATION else "privileged"
            if rank < _trust_rank(required):
                return _deny(
                    operation,
                    reason=(
                        f"SESSION_STATE write requires {required!r} trust level "
                        f"(mode={mode.value}); got {trust!r}"
                    ),
                    effective_view=view.value,
                    effective_scope="session_state_write_denied",
                    mitigation="deny",
                )
        else:
            if rank < _trust_rank("trusted"):
                return _deny(
                    operation,
                    reason=(
                        f"SESSION_STATE read requires 'trusted' trust level; "
                        f"got {trust!r}"
                    ),
                    effective_view=view.value,
                    effective_scope="session_state_read_denied",
                    mitigation="deny",
                )
        return _allow(operation, view=view, scope="session_state")

    if view is MemoryView.VERIFIED_ARCHIVE:
        if is_write_op:
            # CONSOLIDATION mode: trusted+ may write; otherwise no write allowed.
            if mode is ExecutionMode.CONSOLIDATION and rank >= _trust_rank("trusted"):
                return _allow(operation, view=view, scope="verified_archive_consolidation_write")
            return _deny(
                operation,
                reason=(
                    f"VERIFIED_ARCHIVE is read-only "
                    f"(mode={mode.value}, trust_level={trust!r})"
                ),
                effective_view=view.value,
                effective_scope="verified_archive_write_denied",
                mitigation="deny",
            )
        if rank < _trust_rank("trusted"):
            return _deny(
                operation,
                reason=(
                    f"VERIFIED_ARCHIVE read requires 'trusted' trust level; "
                    f"got {trust!r}"
                ),
                effective_view=view.value,
                effective_scope="verified_archive_read_denied",
                mitigation="deny",
            )
        return _allow(operation, view=view, scope="verified_archive")

    if view is MemoryView.PROVISIONAL_ARCHIVE:
        if is_write_op and rank < _trust_rank("trusted"):
            return _deny(
                operation,
                reason=(
                    f"PROVISIONAL_ARCHIVE write requires 'trusted' trust level; "
                    f"got {trust!r}"
                ),
                effective_view=view.value,
                effective_scope="provisional_archive_write_denied",
                mitigation="deny",
            )
        if not is_write_op and rank < _trust_rank("provisional"):
            return _deny(
                operation,
                reason=(
                    f"PROVISIONAL_ARCHIVE read requires 'provisional' trust level; "
                    f"got {trust!r}"
                ),
                effective_view=view.value,
                effective_scope="provisional_archive_read_denied",
                mitigation="deny",
            )
        return _allow(operation, view=view, scope="provisional_archive")

    if view is MemoryView.QUARANTINED:
        # General case: privileged read-only.
        # AUDIT_REVIEW trusted+ path is handled above in before_op.
        if is_write_op or rank < _trust_rank("privileged"):
            return _deny(
                operation,
                reason=(
                    f"QUARANTINED view requires 'privileged' trust level for read "
                    f"and denies all writes; "
                    f"action={operation.action.value!r}, trust_level={trust!r}"
                ),
                effective_view=view.value,
                effective_scope="quarantined_denied",
                mitigation="deny",
            )
        return _allow(operation, view=view, scope="quarantined_privileged_read")

    # Unknown view: fail closed.
    return _deny(
        operation,
        reason=f"unknown memory view: {view!r}",
        effective_view=str(view),
        effective_scope="unknown",
        mitigation="deny",
    )


# ------------------------------------------------------------------
# Context extraction helpers
# ------------------------------------------------------------------


def _resolve_context(
    context: MemoryPolicyContext | None,
) -> tuple[MemoryView, ExecutionMode, str]:
    """Extract (view, mode, trust_level) from *context*, using safe defaults."""
    if context is None:
        return MemoryView.LOCAL_WORKING, ExecutionMode.LIVE, "untrusted"
    return context.memory_view, context.execution_mode, context.trust_level


# ------------------------------------------------------------------
# Decision constructors
# ------------------------------------------------------------------


def _allow(
    operation: MemoryOperation,
    *,
    view: MemoryView | None = None,
    scope: str = "",
) -> MemoryGovernanceDecision:
    threat = ThreatContext(
        effective_view=view.value if view is not None else "",
        effective_scope=scope,
    )
    return MemoryGovernanceDecision(
        verdict=GovernanceVerdict.ALLOW,
        reason="access granted",
        policy_id=_POLICY_ID,
        operation=operation,
        threat_context=threat,
    )


def _deny(
    operation: MemoryOperation,
    *,
    reason: str,
    effective_view: str = "",
    effective_scope: str = "",
    mitigation: str = "deny",
) -> MemoryGovernanceDecision:
    threat = ThreatContext(
        threat_hypothesis="unauthorized memory view access",
        mitigation_applied=mitigation,
        effective_view=effective_view,
        effective_scope=effective_scope,
    )
    return MemoryGovernanceDecision(
        verdict=GovernanceVerdict.DENY,
        reason=reason,
        policy_id=_POLICY_ID,
        operation=operation,
        threat_context=threat,
    )
