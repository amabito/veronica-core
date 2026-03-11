"""Audit bridge for governance decisions.

Takes a DecisionEnvelope and emits a signed audit event to an AuditLog
for decisions in the governance-relevant set: HALT, DEGRADE, QUARANTINE.

No reasoning, no AI. Pure deterministic bridge.
"""

from __future__ import annotations

__all__ = [
    "emit_governance_event",
    "should_emit",
]

from veronica_core.kernel.decision import DecisionEnvelope
from veronica_core.audit.log import AuditLog

# Decisions that warrant a governance audit event.
_GOVERNANCE_DECISIONS: frozenset[str] = frozenset({"HALT", "DEGRADE", "QUARANTINE"})


def should_emit(decision: str) -> bool:
    """Return True when *decision* warrants a governance audit event.

    Only HALT, DEGRADE, and QUARANTINE are governance-relevant.

    Args:
        decision: Decision string from a DecisionEnvelope.

    Returns:
        True for HALT, DEGRADE, QUARANTINE; False for all other values.
    """
    return decision in _GOVERNANCE_DECISIONS


def emit_governance_event(
    envelope: DecisionEnvelope,
    audit_log: AuditLog,
) -> bool:
    """Emit a governance audit event for relevant decisions.

    Only emits for HALT, DEGRADE, and QUARANTINE decisions.  All other
    decisions are silently skipped and False is returned.

    Args:
        envelope: DecisionEnvelope carrying the governance decision.
        audit_log: AuditLog to receive the governance event.

    Returns:
        True if an event was emitted, False if the decision was not
        governance-relevant (ALLOW, DENY, RETRY, QUEUE, etc.).
    """
    if not should_emit(envelope.decision):
        return False

    event_type = f"GOVERNANCE_{envelope.decision}"

    audit_log.write_governance_event(
        event_type=event_type,
        decision=envelope.decision,
        reason_code=envelope.reason_code,
        reason=envelope.reason,
        audit_id=envelope.audit_id,
        policy_hash=envelope.policy_hash,
        policy_epoch=envelope.policy_epoch,
        issuer=envelope.issuer,
        metadata=dict(envelope.metadata) if envelope.metadata else None,
    )

    return True
